#!/usr/bin/env python3

import importlib
import random
import time
from collections import defaultdict
from sqlite3 import Connection
from typing import List

from server import app, database, metrics, reloader
from server.models import Flag, FlagStatus, SubmitResult

from .MessageAnnouncer import FlagAnnouncer

flag_ann = FlagAnnouncer()


def _update_metrics(db: Connection, processed_flags: List[Flag]):

    # Update flags
    queued_flags = db.execute(
        "SELECT COUNT(*) as n" " FROM flags" " WHERE status = ?" " GROUP BY status",
        (FlagStatus.QUEUED.name,),
    ).fetchone()

    if queued_flags:
        metrics.QUEUED_FLAGS.set(queued_flags["n"])
    else:
        metrics.QUEUED_FLAGS.set(0)

    for flag in processed_flags:
        metrics.FLAGS[flag.status].labels(sploit=flag.sploit, team=flag.team).inc()


def get_fair_share(groups, limit):
    if not groups:
        return []

    groups = sorted(groups, key=len)
    places_left = limit
    group_count = len(groups)
    fair_share = places_left // group_count

    result = []
    residuals = []
    for group in groups:
        if len(group) <= fair_share:
            result += group

            places_left -= len(group)
            group_count -= 1
            if group_count > 0:
                fair_share = places_left // group_count
            # The fair share could have increased because the processed group
            # had a few elements. Sorting order guarantees that the smaller
            # groups will be processed first.
        else:
            selected = random.sample(group, fair_share + 1)
            result += selected[:-1]
            residuals.append(selected[-1])
    result += random.sample(residuals, min(limit - len(result), len(residuals)))

    random.shuffle(result)
    return result


def submit_flags(flags, config):
    module = importlib.import_module("server.protocols." + config["SYSTEM_PROTOCOL"])

    try:
        return list(module.submit_flags(flags, config))
    except Exception as e:
        message = f"{type(e).__name__}: {str(e)}"
        app.logger.exception("Exception on submitting flags")
        return [SubmitResult(item.flag, FlagStatus.QUEUED, message) for item in flags]


def run_loop():
    app.logger.info("Starting submit loop")
    with app.app_context():
        db = database.get(context_bound=False)

    old_flags = db.execute(
        "SELECT * FROM flags WHERE status != ?", (FlagStatus.QUEUED.name,)
    ).fetchall()

    old_flags = [
        Flag(
            item["flag"],
            item["sploit"],
            item["team"],
            item["time"],
            FlagStatus[item["status"]],
            item["checksystem_response"],
            item["sent_cycle"],
        )
        for item in old_flags
    ]

    _update_metrics(db, old_flags)

    cycle = db.execute("SELECT MAX(sent_cycle) AS last_cycle FROM flags").fetchone()[
        "last_cycle"
    ]
    if not cycle:
        cycle = 0

    while True:
        cycle += 1

        submit_start_time = time.time()

        config = reloader.get_config()

        # If flag time > FLAG_LIFETIME set as SKIPPED
        skip_time = round(submit_start_time - config["FLAG_LIFETIME"])
        db.execute(
            "UPDATE flags SET status = ? WHERE status = ? AND time < ?",
            (FlagStatus.SKIPPED.name, FlagStatus.QUEUED.name, skip_time),
        )
        db.commit()

        # Get queued flags
        cursor = db.execute(
            "SELECT * FROM flags WHERE status = ?", (FlagStatus.QUEUED.name,)
        )
        queued_flags = [Flag(**item) for item in cursor.fetchall()]
        queued_flags_len = len(queued_flags)

        if queued_flags:
            grouped_flags = defaultdict(list)
            for item in queued_flags:
                grouped_flags[item.sploit, item.team].append(item)

            processed_flags = get_fair_share(
                grouped_flags.values(), config["SUBMIT_FLAG_LIMIT"]
            )

            app.logger.debug(
                "Submitting %d flags (out of %d in queue)",
                len(processed_flags),
                queued_flags_len,
            )
            # Send flags to gameserver
            results = submit_flags(processed_flags, config)

            rows = [
                (item.status.name, item.checksystem_response, cycle, item.flag)
                for item in results
            ]
            db.executemany(
                "UPDATE flags "
                "SET status = ?, checksystem_response = ?, sent_cycle = ? "
                "WHERE flag = ?",
                rows,
            )
            db.commit()

            flags_status = {result.flag: result.status for result in results}

            processed_flags = list(
                map(
                    lambda item: Flag(
                        item.flag,
                        item.sploit,
                        item.team,
                        item.time,
                        flags_status[item.flag],
                        item.checksystem_response,
                        item.sent_cycle,
                    ),
                    processed_flags,
                )
            )

            # Count sent flags
            sent_flags = list(
                filter(lambda x: x.status != FlagStatus.SKIPPED, processed_flags)
            )
            app.logger.info(
                "Submitted %d flags (out of %d in queue)",
                len(sent_flags),
                queued_flags_len,
            )
            _update_metrics(db, processed_flags)

            flag_ann.announce((cycle, processed_flags))

        submit_spent = time.time() - submit_start_time
        metrics.SUBMITTER_LATENCY.observe(submit_spent)

        app.logger.debug(
            "Took %f seconds. The period is %d seconds",
            submit_spent,
            config["SUBMIT_PERIOD"],
        )

        if config["SUBMIT_PERIOD"] > submit_spent:
            sleep_time = config["SUBMIT_PERIOD"] - submit_spent

            app.logger.debug("Sleeping for %.2f seconds...", sleep_time)
            time.sleep(sleep_time)


if __name__ == "__main__":
    run_loop()
