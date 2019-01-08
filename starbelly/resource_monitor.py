from collections import deque
from datetime import datetime, timezone
import logging
import math
import os
from time import time
from uuid import UUID

import psutil
import trio

from protobuf.server_pb2 import ServerMessage


logger = logging.getLogger(__name__)


class ResourceMonitor:
    '''
    Keep track of consumption and usage statistics for various resources.
    '''

    # The number of frames to buffer. At one frame per second, this buffers 5
    # minutes of resource data.
    FRAME_BUFFER = 300

    def __init__(self, interval, buffer_size, crawl_manager, rate_limiter):
        '''
        Constructor.

        :param float interval: The number of seconds to wait between
            measurements.
        :param int buffer_size: The number of measurements to store in the
            internal buffer.
        :param crawl_manager:
        :param starbelly.rate_limiter.RateLimiter rate_limiter:
        '''
        self._interval = interval
        # self._crawl_manager = crawl_manager
        self._rate_limiter = rate_limiter
        self._measurements = deque(maxlen=buffer_size)
        self._channels = list()

    def get_channel(self, channel_size):
        '''
        Get a statistics channel. The resource monitor will send measurements to
        this channel until the receive end is closed. Note that if the channel
        is full, the resource monitor does not block! It will drop messages
        instead.

        :param int channel_size: The size of the channel to create.
        :returns: A channel that will receive resource statistics at regular
            intervals.
        :rtype: trio.ReceiveChannel
        '''
        logger.debug('Creating new channel with size=%d', channel_size)
        send_channel, recv_channel = trio.open_memory_channel(channel_size)
        self._channels.append(send_channel)
        return recv_channel

    def history(self, n=None):
        '''
        A generator that yields the most recent ``n`` measurements.

        :param int n: The number of measurements to retrieve. If ``n`` is None
            or there are fewer than ``n`` measurements, return all measurements.
        :returns: A generator.
        '''
        history_iter = iter(self._measurements)
        # A deque can't be sliced, so we have to do some extra work to return
        # measurements from the end.
        if n is not None:
            for _ in range(len(self._measurements) - n):
                next(history_iter)
        yield from history_iter

    async def run(self):
        '''
        Run the resource monitor.

        :returns: Runs until cancelled.
        '''
        next_run = trio.current_time() + self._interval
        while True:
            measurement = self._measure()
            self._measurements.append(measurement)
            to_remove = set()
            for channel in self._channels:
                try:
                    channel.send_nowait(measurement)
                except trio.WouldBlock:
                    continue
                except trio.BrokenResourceError:
                    to_remove.add(channel)
            for channel in to_remove:
                logger.debug('Removing closed channel')
                self._channels.remove(channel)
            next_run += self._interval
            await trio.sleep(next_run - trio.current_time())

    def _measure(self):
        '''
        TODO uncomment crawl

        :returns: A set of measurements.
        :rtype: dict
        '''
        measurement = dict()
        measurement['timestamp'] = datetime.now(timezone.utc)

        # CPUs
        measurement['cpus'] = psutil.cpu_percent(percpu=True)

        # Memory
        vm = psutil.virtual_memory()
        measurement['memory_used'] = vm.used
        measurement['memory_total'] = vm.total

        # Disks
        measurement['disks'] = list()
        for partition in psutil.disk_partitions():
            disk = dict()
            disk['mount'] = partition.mountpoint
            usage = psutil.disk_usage(disk['mount'])
            disk['used'] = usage.used
            disk['total'] = usage.total
            measurement['disks'].append(disk)

        # Networks
        measurement['networks'] = list()
        for name, nic in psutil.net_io_counters(pernic=True).items():
            net = dict()
            net['name'] = name
            net['sent'] = nic.bytes_sent
            net['received'] = nic.bytes_recv
            measurement['networks'].append(net)

        # Crawls
        # measurement['crawls'] = list()
        # for crawl_stat in self._crawl_manager.get_stats():
        #     crawl = dict()
        #     crawl['job_id'] = UUID(crawl_stat.job_id).bytes
        #     crawl['frontier'] = crawl_stat.frontier
        #     crawl['pending'] = crawl_stat.pending
        #     crawl['extraction'] = crawl_stat.extraction
        #     measurement['crawls'].append(crawl)

        # Rate limiter
        measurement['rate_limiter_count'] = self._rate_limiter.item_count

        return measurement
