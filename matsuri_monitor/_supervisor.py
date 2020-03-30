import gzip
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

import tornado.ioloop
import tornado.options
from cachetools import TTLCache, cached

from matsuri_monitor import chat, clients

tornado.options.define('history-days', default=7, type=int, help='Number of days of history to save')
tornado.options.define('archives-dir', default=Path('archives'), type=Path, help='Path to save archive JSONs')
tornado.options.define('dump-chat', default=False, type=bool, help='Also dump all stream comments to archive dir')


class Supervisor:

    def __init__(self, interval: float):
        """init

        Parameters
        ----------
        interval
            Update interval in seconds
        """
        super().__init__()
        self.interval = interval
        self.jetri = clients.Jetri()
        self.live_monitors: Dict[str, Monitor] = OrderedDict()
        self.archive_reports: List[chat.LiveReport] = []
        self.groupers = chat.Grouper.load()
        tornado.options.options.archives_dir.mkdir(exist_ok=True)

    def update(self, current_ioloop: tornado.ioloop.IOLoop = None):
        """Periodic update of overall app state

        Checks for new lives, prunes old ones and adds them to the archives, and refreshes groupers

        Parameters
        ----------
        current_ioloop
            The IOLoop to use for this process and when spawning monitor processes
        """
        if current_ioloop is None:
            current_ioloop = tornado.ioloop.IOLoop.current()

        # Refresh groupers
        new_groupers = chat.Grouper.load()
        if new_groupers != self.groupers:
            for monitor in self.live_monitors.values():
                monitor.report.set_groupers(new_groupers)
            self.groupers = new_groupers

        # Clean up terminated monitors (including those that terminated with an error)
        to_delete = []

        for video_id, monitor in self.live_monitors.items():
            if not monitor.is_running:
                to_delete.append(video_id)

        for video_id in to_delete:
            del self.live_monitors[video_id]

        # Refresh currently live list and find lives to start and terminate
        self.jetri.update()

        currently_live = set(self.jetri.currently_live)
        currently_monitored = set(self.live_monitors.keys())

        new_lives = currently_live - currently_monitored
        stopped_lives = currently_monitored - currently_live

        # Start new lives
        for video_id in new_lives:
            info = self.jetri.get_live_info(video_id)

            report = chat.LiveReport(info)
            report.set_groupers(self.groupers)

            monitor = clients.Monitor(info, report)
            monitor.start(current_ioloop)

            self.live_monitors[video_id] = monitor

        # Send terminate signal to finished lives and archive their reports
        for video_id in stopped_lives:
            monitor = self.live_monitors[video_id]
            monitor.terminate()

            report = monitor.report

            if len(report) > 0:
                report_datetime = datetime.fromtimestamp(report.info.start_timestamp).isoformat(timespec='seconds')
                report_basename = f'{report_datetime}_{report.info.id}'.replace(':', '')
                report_path = tornado.options.options.archives_dir / f'{report_basename}.json.gz'

                if tornado.options.options.dump_chat:
                    messages_json = [msg.json() for msg in report.messages]
                    messages_path = tornado.options.options.archives_dir / f'{report_basename}_chat.json.gz'

                    with gzip.open(messages_path, 'wt') as dump_file:
                        json.dump(messages_json, dump_file)

                report.finalize()

                with gzip.open(report_path, 'wt') as report_file:
                    json.dump(report.json(), report_file)

                self.archive_reports.append(report)

        # Remove old reports from memory
        self.prune()

    def prune(self):
        """Remove old reports"""
        timestamp_now = datetime.utcnow().timestamp()
        cutoff = timestamp_now - timedelta(days=tornado.options.options.history_days).total_seconds()
        pruned_reports = list(filter(lambda r: r.info.start_timestamp > cutoff, self.archive_reports))

        self.archive_reports = pruned_reports

    @cached(TTLCache(1, 5))
    def live_json(self) -> dict:
        """JSON object containing reports of all currently live streams"""
        return {'reports': [monitor.report.json() for monitor in self.live_monitors.values()]}

    @cached(TTLCache(1, 30))
    def archive_json(self) -> dict:
        """JSON object containing reports of all archived live streams"""
        return {'reports': [report.json() for report in self.archive_reports]}

    def get_scheduler(self) -> tornado.ioloop.PeriodicCallback:
        """Get scheduler that periodically updates the app state"""
        def update_async():
            current_ioloop = tornado.ioloop.IOLoop.current()
            current_ioloop.run_in_executor(None, self.update, current_ioloop)

        return tornado.ioloop.PeriodicCallback(update_async, self.interval * 1000)
