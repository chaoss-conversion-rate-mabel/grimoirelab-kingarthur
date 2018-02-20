# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#     Alvaro del Castillo San Felix <acs@bitergia.com>
#

import functools
import logging

import rq
import pickle

import perceval
import perceval.backends
import perceval.archive

from grimoirelab.toolkit.datetime import unixtime_to_datetime

from ._version import __version__
from .errors import NotFoundError


logger = logging.getLogger(__name__)


def metadata(func):
    """Add metadata to an item.

    Decorator that adds metadata to Perceval items such as the
    identifier of the job that generated it or the version of
    the system. The contents from the original item will
    be stored under the 'data' keyword.

    Take into account that this function only can be called from
    a `PercevalJob` class due it needs access to some attributes
    and methods of this class.
    """
    @functools.wraps(func)
    def decorator(self, *args, **kwargs):
        for item in func(self, *args, **kwargs):
            item['arthur_version'] = __version__
            item['job_id'] = self.job_id
            yield item
    return decorator


class JobResult:
    """Class to store the result of a Perceval job.

    It stores useful data such as the taks_id, the UUID of the last
    item generated or the number of items fetched by the backend.

    :param job_id: job identifier
    :param task_id: identitifer of the task linked to this job
    :param backend: backend used to fetch the items
    :param category: category of the fetched items
    :param last_uuid: UUID of the last item
    :param max_date: maximum date fetched among items
    :param nitems: number of items fetched by the backend
    :param offset: maximum offset fetched among items
    :param nresumed: number of time the job was resumed
    """
    def __init__(self, job_id, task_id, backend, category, last_uuid,
                 max_date, nitems, offset=None, nresumed=0):
        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.category = category
        self.last_uuid = last_uuid
        self.max_date = max_date
        self.nitems = nitems
        self.offset = offset
        self.nresumed = nresumed


class PercevalJob:
    """Class for wrapping Perceval jobs.

    Wrapper for running and executing Perceval backends. The items
    generated by the execution of a backend will be stored on the
    Redis queue named `qitems`. The result of the job can be obtained
    accesing to the property `result` of this object.

    :param job_id: job identifier
    :param task_id: identitifer of the task linked to this job
    :param backend: name of the backend to execute
    :param conn: connection with a Redis database
    :param qitems: name of the queue where items will be stored

    :rasises NotFoundError: raised when the backend is not avaliable
        in Perceval
    """
    def __init__(self, job_id, task_id, backend, category, conn, qitems):
        try:
            self._bklass = perceval.find_backends(perceval.backends)[0][backend]
        except KeyError:
            raise NotFoundError(element=backend)

        self.job_id = job_id
        self.task_id = task_id
        self.backend = backend
        self.conn = conn
        self.qitems = qitems
        self.retries = 0
        self.archive_manager = None
        self.category = category
        self._result = JobResult(self.job_id, self.task_id, self.backend, self.category,
                                 None, None, 0, offset=None,
                                 nresumed=0)

    @property
    def result(self):
        return self._result

    def initialize_archive_manager(self, archive_path):
        """Initialize the archive manager.

        :param archive_path: path where the archive manager is located
        """
        if archive_path == "":
            raise ValueError("Archive manager path cannot be empty")

        if archive_path:
            self.archive_manager = perceval.archive.ArchiveManager(archive_path)

    def run(self, backend_args, archive_args=None, resume=False):
        """Run the backend with the given parameters.

        The method will run the backend assigned to this job,
        storing the fetched items in a Redis queue. The ongoing
        status of the job, can be accessed through the property
        `result`. When `resume` is set, the job will start from
        the last execution, overwriting 'from_date' and 'offset'
        parameters, if needed.

        Setting to `True` the parameter `fetch_from_archive`, items can
        be fetched from the archive assigned to this job.

        Any exception during the execution of the process will
        be raised.

        :param backend_args: parameters used to un the backend
        :param archive_args: archive arguments
        :param resume: fetch items starting where the last
            execution stopped
        """
        args = backend_args.copy()

        if archive_args:
            self.initialize_archive_manager(archive_args['archive_path'])

        if not resume:
            self._result = JobResult(self.job_id, self.task_id, self.backend, self.category,
                                     None, None, 0, offset=None,
                                     nresumed=0)
        else:
            if self.result.max_date:
                args['from_date'] = unixtime_to_datetime(self.result.max_date)
            if self.result.offset:
                args['offset'] = self.result.offset
            self._result.nresumed += 1

        for item in self._execute(args, archive_args):
            self.conn.rpush(self.qitems, pickle.dumps(item))

            self._result.nitems += 1
            self._result.last_uuid = item['uuid']

            if not self.result.max_date or self.result.max_date < item['updated_on']:
                self._result.max_date = item['updated_on']
            if 'offset' in item:
                self._result.offset = item['offset']

    def has_archiving(self):
        """Returns if the job supports items archiving"""

        return self._bklass.has_archiving()

    def has_resuming(self):
        """Returns if the job can be resumed when it fails"""

        return self._bklass.has_resuming()

    @metadata
    def _execute(self, backend_args, archive_args):
        """Execute a backend of Perceval.

        Run the backend of Perceval assigned to this job using the
        given arguments. It will raise an `AttributeError` when any of
        the required parameters to run the backend are not found.
        Other exceptions related to the execution of the backend
        will be raised too.

        This method will return an iterator of the items fetched
        by the backend. These items will include some metadata
        related to this job.

        It will also be possible to retrieve the items from the
        archive setting to `True` the parameter `fetch_from_archive`.

        :param bakend_args: arguments to execute the backend
        :param archive_args: archive arguments

        :returns: iterator of items fetched by the backend

        :raises AttributeError: raised when any of the required
            parameters is not found
        """

        if not archive_args or not archive_args['fetch_from_archive']:
            return perceval.fetch(self._bklass, backend_args, manager=self.archive_manager)
        else:
            return perceval.fetch_from_archive(self._bklass, backend_args, self.archive_manager,
                                               self.category, archive_args['archived_after'])


def execute_perceval_job(backend, backend_args, qitems, task_id, category,
                         archive_args=None, sched_args=None):
    """Execute a Perceval job on RQ.

    The items fetched during the process will be stored in a
    Redis queue named `queue`.

    Setting the parameter `archive_path`, raw data will be stored
    with the archive manager. The contents from the archive can
    be retrieved setting the pameter `fetch_from_archive` to `True`,
    too. Take into account this behaviour will be only available
    when the backend supports the use of the archive. If archiving
    is not supported, an `AttributeErrror` exception will be raised.

    :param backend: backend to execute
    :param bakend_args: dict of arguments for running the backend
    :param qitems: name of the RQ queue used to store the items
    :param task_id: identifier of the task linked to this job
    :param category: category of the items to retrieve
    :param archive_args: archive arguments
    :param sched_args: scheduler arguments

    :returns: a `JobResult` instance

    :raises NotFoundError: raised when the backend is not found
    :raises AttributeError: raised when archiving is not supported but
        any of the archive parameters were set
    """
    rq_job = rq.get_current_job()

    job = PercevalJob(rq_job.id, task_id, backend, category,
                      rq_job.connection, qitems)

    logger.debug("Running job #%s (task: %s) (%s) (cat:%s)",
                 job.job_id, task_id, backend, category)

    if not job.has_archiving() and archive_args:
        raise AttributeError("archive attributes set but archive is not supported")

    run_job = True
    resume = False
    failures = 0

    while run_job:
        try:
            job.run(backend_args, archive_args=archive_args, resume=resume)
        except AttributeError as e:
            raise e
        except Exception as e:
            logger.debug("Error running job %s (%s) - %s",
                         job.job_id, backend, str(e))
            failures += 1

            if not job.has_resuming() or failures >= sched_args['max_retries']:
                logger.error("Cancelling job #%s (task: %s) (%s)",
                             job.job_id, task_id, backend)
                raise e

            logger.warning("Resuming job #%s (task: %s) (%s) due to a failure (n %s, max %s)",
                           job.job_id, task_id, backend, failures, sched_args['max_retries'])
            resume = True
        else:
            # No failure, do not retry
            run_job = False

    result = job.result

    logger.debug("Job #%s (task: %s) completed (%s) - %s items (%s) fetched",
                 result.job_id, task_id, result.backend, str(result.nitems), result.category)

    return result
