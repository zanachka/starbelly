import logging

from rethinkdb import RethinkDB
import trio

from .db import (
    BootstrapDb,
    CrawlFrontierDb,
    CrawlManagerDb,
    CrawlExtractorDb,
    CrawlStorageDb,
    LoginDb,
    ScheduleDb,
    ServerDb,
    SubscriptionDb,
)
from .config import get_config, get_path
from .job import CrawlManager, StatsTracker, RunState
from .downloader import Downloader
from .rate_limiter import RateLimiter
from .resource_monitor import ResourceMonitor
from .robots import RobotsTxtManager
from .schedule import Scheduler
from .server import Server
from .subscription import SubscriptionManager


logger = logging.getLogger(__name__)


class Bootstrap:
    ''' Main class for bootstrapping the crawler. '''
    def __init__(self, config, args):
        '''
        Constructor.

        :param config: Output of config parser.
        :param args: Output of argparse.
        :param
        '''
        self._args = args
        self._config = config

    def run(self):
        ''' Run the main task on the event loop. '''
        logger.info('Starbelly is starting...')
        try:
            trio.run(self._main,
                restrict_keyboard_interrupt_to_checkpoints=True)
        except KeyboardInterrupt:
            logger.warning('Quitting due to KeyboardInterrupt')
        logger.info('Starbelly has stopped.')

    def _db_pool(self, nursery):
        '''
        Create a database connectoin pool.

        :param nursery: A Trio nursery to spawn database connections in.
        :returns: A RethinkDB connection pool.
        '''
        r = RethinkDB()
        r.set_loop_type('trio')
        db_config = self._config['database']
        return r.ConnectionPool(
            host=db_config['host'],
            port=db_config['port'],
            db=db_config['db'],
            user=db_config['user'],
            password=db_config['password'],
            nursery=nursery
        )

    async def _main(self):
        '''
        The main task.

        :returns: This function runs until cancelled.
        '''
        # Create db pool & objects
        async with trio.open_nursery() as nursery:
            db_pool = self._db_pool(nursery)
            bootstrap_db = BootstrapDb(db_pool)
            crawl_db = CrawlManagerDb(db_pool)
            extractor_db = CrawlExtractorDb(db_pool)
            frontier_db = CrawlFrontierDb(db_pool)
            login_db = LoginDb(db_pool)
            schedule_db = ScheduleDb(db_pool)
            storage_db = CrawlStorageDb(db_pool)
            logging.info('Doing startup check...')
            await bootstrap_db.startup_check()

            # Create a rate limiter
            rate_limiter = RateLimiter(capacity=1_000)
            logger.info('Initializing rate limiter...')
            rate_limits = await bootstrap_db.get_rate_limits()
            for rate_limit in rate_limits:
                rate_limiter.set_rate_limit(rate_limit['token'],
                    rate_limit['delay'])
            logger.info('Rate limiter is initialized.')

            # Create a robots.txt manager
            to_robots, _ = trio.open_memory_channel(0)
            _, from_robots = trio.open_memory_channel(0)
            robots_txt_manager = RobotsTxtManager(db_pool, to_robots,
                from_robots)

            # Create a crawl manager
            stats_tracker = StatsTracker()
            crawl_manager = CrawlManager(rate_limiter, stats_tracker,
                robots_txt_manager, crawl_db, frontier_db, extractor_db,
                storage_db, login_db)

            # Create a resource monitor: one sample per second and keep 1 minute of
            # history.
            resource_monitor = ResourceMonitor(interval=1.0, buffer_size=60,
                crawl_resources_fn=crawl_manager.get_resource_usage,
                rate_limiter=rate_limiter)

            # Create a scheduler
            scheduler = Scheduler(schedule_db, crawl_manager)

            # Create a server
            server_db = ServerDb(db_pool)
            subscription_db = SubscriptionDb(db_pool)
            server = Server(self._args.ip, self._args.port, server_db,
                subscription_db, crawl_manager, rate_limiter, resource_monitor,
                stats_tracker, scheduler)

            # Run all the components
            await nursery.start(crawl_manager.run)
            nursery.start_soon(rate_limiter.run)
            nursery.start_soon(resource_monitor.run)
            nursery.start_soon(scheduler.run)
            await nursery.start(server.run)