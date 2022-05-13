from peewee import (
    Model,
    SqliteDatabase,
    CharField,
    DateTimeField,
    IntegerField,
    ForeignKeyField,
    BooleanField,
    FloatField,
    DateField,
    TimeField,
    CompositeKey,
    JOIN,
    Check,
)
from collections import defaultdict
import datetime
from enum import IntEnum, auto
import sys
import inspect
import os
import yaml
import time


# Defer initialization
class DB:
    # Adding foreign_keys pragma is necessary for ON DELETE behavior
    queues = SqliteDatabase(None, pragmas={"foreign_keys": 1})


DEFAULT_QUEUE = "local"
ARCHIVE_QUEUE = "archive"


class StorageDetails(Model):
    schemaVersion = CharField(unique=True)

    class Meta:
        database = DB.queues


class Queue(Model):
    name = CharField(unique=True)
    created = DateTimeField(default=datetime.datetime.now)
    rank = FloatField()
    addr = CharField(null=True)  # null == local queue
    strategy = CharField()

    class Meta:
        database = DB.queues

    def as_dict(self):
        q = dict(
            name=self.name,
            addr=self.addr,
            strategy=self.strategy,
        )
        return q


class Job(Model):
    queue = ForeignKeyField(Queue, backref="jobs", on_delete="CASCADE")
    name = CharField()
    draft = BooleanField(default=True)
    rank = FloatField()
    count = IntegerField(default=1, constraints=[Check("count > 0")])
    remaining = IntegerField(
        default=1, constraints=[Check("remaining >= 0"), Check("remaining <= count")]
    )
    created = DateTimeField(default=datetime.datetime.now)

    class Meta:
        database = DB.queues

    def normalize(self):
        # Return True if there's valid work
        if self.remaining == 0:
            return self.has_incomplete_sets()
        if self.has_incomplete_sets():
            return True
        return self.decrement(save=True)

    def decrement(self, save=False) -> bool:
        self.remaining = max(self.remaining - 1, 0)
        if save:
            self.save()
        if self.remaining > 0:
            if save:
                Set.update(remaining=Set.count).where(Set.job == self).execute()
            else:
                for s in self.sets:
                    s.remaining = s.count
        return self.has_work()

    def has_incomplete_sets(self) -> bool:
        print("has_incomplete_sets")
        for s in self.sets:
            print(s.remaining)
            if s.remaining > 0:
                return True
        return False

    def has_work(self) -> bool:
        return self.has_incomplete_sets() or self.remaining > 0

    @classmethod
    def from_dict(self, data: dict):
        j = Job(**data)
        j.sets = [Set(**s) for s in data["sets"]]
        return j

    def as_dict(self):
        sets = list(self.sets)
        sets.sort(key=lambda s: s.rank)
        sets = [s.as_dict() for s in sets]
        d = dict(
            name=self.name,
            count=self.count,
            draft=self.draft,
            sets=sets,
            created=self.created,
            id=self.id,
            remaining=self.remaining,
        )
        d["created"] = int(d["created"].timestamp())
        return d


class Set(Model):
    path = CharField()
    sd = BooleanField()
    job = ForeignKeyField(Job, backref="sets", on_delete="CASCADE")
    rank = FloatField()
    count = IntegerField(default=1, constraints=[Check("count > 0")])
    remaining = IntegerField(
        default=1, constraints=[Check("remaining >= 0"), Check("remaining <= count")]
    )

    # This is a CSV of material key strings referencing SpoolManager entities
    # (makes it easier to manage material keys as a single field)
    # It's intentionally NOT a foreign key for this reason.
    material_keys = CharField()

    def materials(self):
        if self.material_keys == "":
            return []
        return self.material_keys.split(",")

    class Meta:
        database = DB.queues

    def decrement(self, save=False):
        self.remaining = max(0, self.remaining - 1)
        if save:
            self.save()  # Save must occur before job is observed
        if not self.job.has_incomplete_sets():
            self.job.decrement(save=save)
        else:
            print("job still has incomplete sets")

    def as_dict(self):
        return dict(
            path=self.path,
            count=self.count,
            materials=self.materials(),
            id=self.id,
            rank=self.rank,
            sd=self.sd,
            remaining=self.remaining,
        )


class Run(Model):
    queueName = CharField()
    jobName = CharField()
    job = ForeignKeyField(Job, backref="runs", on_delete="CASCADE")
    path = CharField()
    start = DateTimeField(default=datetime.datetime.now)
    end = DateTimeField(null=True)
    result = CharField(null=True)

    class Meta:
        database = DB.queues

    def as_dict(self):
        d = dict(
            start=self.start,
            end=self.end,
            result=self.result,
            id=self.id,
            job_id=self.job.id,
            path=self.path,
            jobName=self.jobName,
            queueName=self.queueName,
        )
        d["start"] = int(d["start"].timestamp())
        if d["end"] is not None:
            d["end"] = int(d["end"].timestamp())
        return d


def file_exists(path: str) -> bool:
    try:
        return os.stat(path).st_size > 0
    except OSError:
        return False


def init(db_path="queues.sqlite3", logger=None):
    db = DB.queues
    needs_init = not file_exists(db_path)
    db.init(None)
    db.init(db_path)
    db.connect()

    if needs_init:
        if logger is not None:
            logger.info("DB needs init")
        DB.queues.create_tables([Queue, Job, Set, Run, StorageDetails])
        StorageDetails.create(schemaVersion="0.0.1")
        Queue.create(name=DEFAULT_QUEUE, strategy="LINEAR", rank=0)
        Queue.create(name=ARCHIVE_QUEUE, strategy="LINEAR", rank=-1)
    else:
        try:
            details = StorageDetails.select().limit(1).execute()[0]
            if logger is not None:
                logger.info("Storage schema version: " + details.schemaVersion)
            if details.schemaVersion != "0.0.1":
                raise Exception("Unknown DB schema version: " + details.schemaVersion)
        except Exception:
            raise Exception("Failed to fetch storage schema details!")


def migrateFromSettings(data: list):
    # Prior to v2.0.0, all state for the plugin was stored in a json-serialized list
    # in OctoPrint settings. This method converts the various forms of the json blob
    # to entries in the database.

    q = Queue.get(name=DEFAULT_QUEUE)
    jr = 0
    for i in data:
        jname = i.get("job", "")
        try:
            j = Job.get(name=jname)
        except Job.DoesNotExist:
            j = Job(queue=q, name=jname, draft=False, rank=jr, count=1, remaining=0)
            jr += 1
        run_num = i.get("run", 0)
        j.count = max(run_num + 1, j.count)
        if i.get("end_ts") is None:
            j.remaining = max(j.count - run_num, j.remaining)
        j.save()

        s = None
        spath = i.get("path", "")
        for js in j.sets:
            if js.path == spath:
                s = js
                break
        if s is None:
            sd = i.get("sd", False)
            if type(sd) == str:
                sd = sd.lower() == "true"
            if type(sd) != bool:
                sd = False
            mkeys = ""
            mats = i.get("materials")
            if mats is not None and len(mats) > 0:
                mkeys = ",".join(mats)
            s = Set(
                path=spath,
                sd=sd,
                job=j,
                rank=len(j.sets),
                count=1,
                remaining=0,
                material_keys=mkeys,
            )
        else:
            if i.get("run") == 0:  # Treat first run as true count
                s.count += 1
            if i.get("end_ts") is None:
                s.remaining += 1
        s.save()

        start_ts = i.get("start_ts")
        end_ts = i.get("end_ts")
        if start_ts is not None:
            start_ts = datetime.datetime.fromtimestamp(start_ts)
            end_ts = i.get("end_ts")
            if end_ts is not None:
                end_ts = datetime.datetime.fromtimestamp(end_ts)
            Run.create(
                queueName=DEFAULT_QUEUE,
                jobName=jname,
                job=j,
                path=spath,
                start=start_ts,
                end=end_ts,
                result=i.get("result"),
            )
