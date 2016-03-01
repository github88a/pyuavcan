#
# Copyright (C) 2014-2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Ben Dyer <ben_dyer@mac.com>
#         Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import division, absolute_import, print_function, unicode_literals
import os
import sys
import time
import binascii
import select
import multiprocessing
import threading
import copy
from logging import getLogger
from .common import DriverError, CANFrame, AbstractDriver
from .timestamp_estimator import TimestampEstimator

try:
    import queue
except ImportError:
    # noinspection PyPep8Naming,PyUnresolvedReferences
    import Queue as queue

logger = getLogger(__name__)

# If PySerial isn't available, we can't support SLCAN
try:
    import serial
except ImportError:
    serial = None
    logger.info("Cannot import PySerial; SLCAN will not be available.")

#
# Constants and defaults
#
RX_QUEUE_SIZE = 100000
TX_QUEUE_SIZE = 1000

TIMESTAMP_OVERFLOW_PERIOD = 60          # Defined by SLCAN protocol

DEFAULT_BITRATE = 1000000
DEFAULT_BAUDRATE = 3000000

ACK_TIMEOUT = 0.5
ACK = b'\r'
NACK = b'\x07'

DEFAULT_MAX_ADAPTER_CLOCK_RATE_ERROR_PPM = 200      # Suits virtually all adapters
DEFAULT_FIXED_RX_DELAY = 0.0002                     # Good for USB, could be higher for UART
DEFAULT_MAX_ESTIMATED_RX_DELAY_TO_RESYNC = 0.1      # When clock divergence exceeds this value, resync

IO_PROCESS_INIT_TIMEOUT = 5


#
# IPC constants
#
IPC_SIGNAL_INIT_OK = 'init_ok'                     # Sent from IO process to the parent process when init is done
IPC_COMMAND_STOP = 'stop'                          # Sent from parent process to the IO process when it's time to exit


#
# Logic of the IO process
#
def _wait_for_ack(conn):
    conn.timeout = ACK_TIMEOUT
    while True:
        b = conn.read(1)
        if not b:
            raise DriverError('SLCAN ACK timeout')
        if b == NACK:
            raise DriverError('SLCAN NACK in response')
        if b == ACK:
            break


def _init_adapter(conn, bitrate):
    speed_code = {
        1000000: 8,
        8000000: 7,
        500000: 6,
        250000: 5,
        125000: 4,
        100000: 3,
        50000: 2,
        20000: 1,
        10000: 0
    }[bitrate if bitrate is not None else DEFAULT_BITRATE]

    # Discarding all input
    time.sleep(0.1)
    conn.flushInput()

    # Setting speed code
    conn.write('S{0:d}\r'.format(speed_code).encode())
    conn.flush()
    _wait_for_ack(conn)

    # Opening the channel
    conn.write(b'O\r')
    conn.flush()
    _wait_for_ack(conn)

    # Discarding all input again
    time.sleep(0.1)
    conn.flushInput()


def _stop_adapter(conn):
    conn.write(b'C\r')
    conn.flush()


# noinspection PyBroadException
def _rx_thread(conn, rx_queue, ts_estimator_mono, ts_estimator_real, termination_condition):
    logger.info('RX thread started')

    py2_compat = sys.version_info[0] < 3
    select_timeout = 0.1
    read_buffer_size = 1024 * 1024      # Arbitrary large number

    buf = bytes()
    while not termination_condition():
        try:
            select.select([conn.fileno()], [], [], select_timeout)

            # Timestamping as soon as possible after unblocking
            local_ts_mono = time.monotonic()
            local_ts_real = time.time()

            # Read as much data as possible in order to avoid RX overrun
            conn.timeout = 0
            buf += conn.read(read_buffer_size)

            # The parsing logic below is heavily optimized for speed
            pos = 0
            buf_len = len(buf)
            while True:
                # Looking for start of the next message, break if not found
                while pos < buf_len and buf[pos] not in b'Tt':
                    pos += 1
                if pos >= buf_len:
                    break

                # Now, pos points to the beginning of the next message - parse it
                try:
                    id_len = 8 if buf[pos] == b'T'[0] else 3

                    available_length = buf_len - pos
                    if available_length < id_len + 2:  # Shortest message is 't<ID>0'
                        break

                    # Parse the header
                    packet_id = int(buf[pos + 1:pos + 1 + id_len].decode(), 16)
                    if py2_compat:
                        packet_len = int(buf[pos + 1 + id_len])             # This version is horribly slow
                    else:
                        packet_len = buf[pos + 1 + id_len] - 48             # Py3 version is faster

                    if packet_len > 8:
                        raise DriverError('Invalid packet length')

                    # All kinds of weird and wonderful stuff
                    # <type> <id> <dlc> <data>         [timestamp] \r
                    # 1      3|8  1     packet_len * 2 [4]         1
                    total_length = 2 + id_len + packet_len * 2 + 1
                    if available_length < total_length:
                        break
                    with_timestamp = buf[pos + total_length - 1] in b'0123456789ABCDEF'
                    if with_timestamp:
                        total_length += 3                                   # 3 not 4 because we don't need \r
                        if available_length < total_length:
                            break

                    packet_data = binascii.a2b_hex(buf[pos + 2 + id_len:pos + 2 + id_len + packet_len * 2])
                    pos += total_length

                    if with_timestamp:
                        ts_hardware = int(buf[pos - 4:pos], 16) * 1e-3
                    else:
                        ts_hardware = None
                except Exception:   # Message is malformed
                    logger.warning('Could not parse SLCAN stream [%r]', buf[pos:], exc_info=True)
                    pos += 1        # Continue from the next position
                else:
                    # Converting the hardware timestamp into the local clock domains
                    if ts_hardware is not None:
                        ts_mono = ts_estimator_mono.update(ts_hardware, local_ts_mono)
                        ts_real = ts_estimator_real.update(ts_hardware, local_ts_real)
                    else:
                        ts_mono = local_ts_mono
                        ts_real = local_ts_real

                    frame = CANFrame(packet_id, packet_data, (id_len == 8), ts_monotonic=ts_mono, ts_real=ts_real)
                    rx_queue.put_nowait(frame)

            # All data that could be parsed is already parsed - discard everything up to the current pos
            buf = buf[pos:]
        except Exception as ex:
            # TODO: handle the case when the port is closed
            logger.error('RX thread error, buffer discarded', exc_info=True)
            # Discarding the buffer
            buf = bytes()
            # Propagating the exception to the parent process
            try:
                rx_queue.put_nowait(ex)
            except Exception:
                pass

    logger.info('RX thread is exiting')


def _send_frame(conn, frame):
    line = '%s%d%s\r' % (('T%08X' if frame.extended else 't%03X') % frame.id,
                         len(frame.data),
                         binascii.b2a_hex(frame.data).decode('ascii'))

    conn.write(line.encode('ascii'))
    conn.flush()


def _tx_thread(conn, rx_queue, tx_queue, termination_condition):
    queue_block_timeout = 0.1

    while not termination_condition():
        try:
            command = tx_queue.get(True, queue_block_timeout)

            if isinstance(command, CANFrame):
                _send_frame(conn, command)
            elif command == IPC_COMMAND_STOP:
                break
            else:
                raise DriverError('IO process received unknown IPC command: %r' % command)
        except queue.Empty:
            continue
        except Exception as ex:
            logger.error('TX thread exception', exc_info=True)
            # Propagating the exception to the parent process
            try:
                rx_queue.put_nowait(ex)
            except Exception:
                pass


# noinspection PyBroadException
def _io_process(device,
                tx_queue,
                rx_queue,
                bitrate=None,
                baudrate=None,
                max_adapter_clock_rate_error_ppm=None,
                fixed_rx_delay=None,
                max_estimated_rx_delay_to_resync=None,
                **_extras):
    logger.info('IO process started with PID %r', os.getpid())

    #
    # This is needed to convert timestamps from hardware clock to local clocks
    #
    if max_adapter_clock_rate_error_ppm is None:
        max_adapter_clock_rate_error = DEFAULT_MAX_ADAPTER_CLOCK_RATE_ERROR_PPM / 1e6
    else:
        max_adapter_clock_rate_error = max_adapter_clock_rate_error_ppm / 1e6

    fixed_rx_delay = fixed_rx_delay if fixed_rx_delay is not None else DEFAULT_FIXED_RX_DELAY
    max_estimated_rx_delay_to_resync = max_estimated_rx_delay_to_resync or DEFAULT_MAX_ESTIMATED_RX_DELAY_TO_RESYNC

    ts_estimator_mono = TimestampEstimator(max_rate_error=max_adapter_clock_rate_error,
                                           source_clock_overflow_period=TIMESTAMP_OVERFLOW_PERIOD,
                                           fixed_delay=fixed_rx_delay,
                                           max_phase_error_to_resync=max_estimated_rx_delay_to_resync)
    ts_estimator_real = copy.deepcopy(ts_estimator_mono)

    #
    # Preparing the RX thread
    #
    should_exit = False

    def rx_thread_wrapper():
        try:
            _rx_thread(conn, rx_queue, ts_estimator_mono, ts_estimator_real, lambda: should_exit)
        except Exception as ex:
            logger.error('RX thread failed, exiting', exc_info=True)
            # Propagating the exception to the parent process
            rx_queue.put_nowait(ex)

    rxthd = threading.Thread(target=rx_thread_wrapper, name='slcan_rx')
    rxthd.daemon = True

    conn = serial.Serial(device, baudrate or DEFAULT_BAUDRATE)

    #
    # Actual work is here
    #
    try:
        _init_adapter(conn, bitrate)

        rxthd.start()

        logger.info('IO process initialization complete')
        rx_queue.put(IPC_SIGNAL_INIT_OK)

        _tx_thread(conn, rx_queue, tx_queue, lambda: (should_exit or not rxthd.is_alive()))
    finally:
        logger.info('IO process is terminating...')
        should_exit = True
        if rxthd.is_alive():
            rxthd.join()

        _stop_adapter(conn)
        conn.close()
        logger.info('IO process is now ready to die, goodbye')


#
# Logic of the main process
#
class SLCAN(AbstractDriver):
    def __init__(self, *args, **kwargs):
        if not serial:
            raise RuntimeError("PySerial not imported; SLCAN is not available. Please install PySerial.")

        super(SLCAN, self).__init__()

        self._rx_queue = multiprocessing.Queue(maxsize=RX_QUEUE_SIZE)
        self._tx_queue = multiprocessing.Queue(maxsize=TX_QUEUE_SIZE)

        kwargs['rx_queue'] = self._rx_queue
        kwargs['tx_queue'] = self._tx_queue

        self._proc = multiprocessing.Process(target=_io_process, name='slcan_io_process', args=args, kwargs=kwargs)
        self._proc.daemon = True
        self._proc.start()

        deadline = time.monotonic() + IO_PROCESS_INIT_TIMEOUT
        while True:
            try:
                sig = self._rx_queue.get(timeout=IO_PROCESS_INIT_TIMEOUT)
                if sig == IPC_SIGNAL_INIT_OK:
                    break
            except queue.Empty:
                pass
            if time.monotonic() > deadline:
                self._tx_queue.put_nowait(IPC_COMMAND_STOP)
                raise DriverError('IO process did not confirm initialization')

        self._check_alive()

    def close(self):
        if self._proc.is_alive():
            self._tx_queue.put(IPC_COMMAND_STOP)
            self._proc.join()

    def __del__(self):
        self.close()

    def _check_alive(self):
        if not self._proc.is_alive():
            raise DriverError('IO process is dead :(')

    def receive(self, timeout=None):
        self._check_alive()
        try:
            # TODO this is a workaround. Zero timeout causes the IPC queue to ALWAYS throw queue.Empty!
            timeout = max(timeout, 0.001)
            obj = self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            return

        if isinstance(obj, CANFrame):
            self._rx_hook(obj)
            return obj
        elif isinstance(obj, Exception):    # Propagating exceptions from the IO process to the main process
            raise obj
        else:
            raise DriverError('Unexpected entity in IPC channel: %r' % obj)

    def send(self, message_id, message, extended=False):
        self._check_alive()
        frame = CANFrame(message_id, message, extended)
        self._tx_queue.put(frame)
        self._tx_hook(frame)
