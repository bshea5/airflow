#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Internal classes for management of dag backfills.

:meta private:
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    desc,
    func,
    select,
)
from sqlalchemy.orm import relationship, validates
from sqlalchemy_jsonfield import JSONField

from airflow.api_connexion.exceptions import NotFound
from airflow.exceptions import AirflowException
from airflow.models.base import Base, StringID
from airflow.models.dag_version import DagVersion
from airflow.settings import json
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.sqlalchemy import UtcDateTime, nulls_first, with_row_locks
from airflow.utils.state import DagRunState
from airflow.utils.types import DagRunTriggeredByType, DagRunType

if TYPE_CHECKING:
    from datetime import datetime

    from airflow.models.dag import DAG
    from airflow.timetables.base import DagRunInfo

log = logging.getLogger(__name__)


class AlreadyRunningBackfill(AirflowException):
    """
    Raised when attempting to create backfill and one already active.

    :meta private:
    """


class ReprocessBehavior(str, Enum):
    """
    Internal enum for setting reprocess behavior in a backfill.

    :meta private:
    """

    FAILED = "failed"
    COMPLETED = "completed"
    NONE = "none"


class Backfill(Base):
    """Model representing a backfill job."""

    __tablename__ = "backfill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dag_id = Column(StringID(), nullable=False)
    from_date = Column(UtcDateTime, nullable=False)
    to_date = Column(UtcDateTime, nullable=False)
    dag_run_conf = Column(JSONField(json=json), nullable=False, default={})
    is_paused = Column(Boolean, default=False)
    """
    Controls whether new dag runs will be created for this backfill.

    Does not pause existing dag runs.
    """
    reprocess_behavior = Column(StringID(), nullable=False, default=ReprocessBehavior.NONE)
    max_active_runs = Column(Integer, default=10, nullable=False)
    created_at = Column(UtcDateTime, default=timezone.utcnow, nullable=False)
    completed_at = Column(UtcDateTime, nullable=True)
    updated_at = Column(UtcDateTime, default=timezone.utcnow, onupdate=timezone.utcnow, nullable=False)

    backfill_dag_run_associations = relationship("BackfillDagRun", back_populates="backfill")

    def __repr__(self):
        return f"Backfill({self.dag_id=}, {self.from_date=}, {self.to_date=})"


class BackfillDagRunExceptionReason(str, Enum):
    """
    Enum for storing reasons why dag run not created.

    :meta private:
    """

    IN_FLIGHT = "in flight"
    ALREADY_EXISTS = "already exists"
    UNKNOWN = "unknown"


class BackfillDagRun(Base):
    """Mapping table between backfill run and dag run."""

    __tablename__ = "backfill_dag_run"
    id = Column(Integer, primary_key=True, autoincrement=True)
    backfill_id = Column(Integer, nullable=False)
    dag_run_id = Column(Integer, nullable=True)
    exception_reason = Column(StringID())
    logical_date = Column(UtcDateTime, nullable=False)
    sort_ordinal = Column(Integer, nullable=False)

    backfill = relationship("Backfill", back_populates="backfill_dag_run_associations")
    dag_run = relationship("DagRun")

    __table_args__ = (
        UniqueConstraint("backfill_id", "dag_run_id", name="ix_bdr_backfill_id_dag_run_id"),
        ForeignKeyConstraint(
            [backfill_id],
            ["backfill.id"],
            name="bdr_backfill_fkey",
            ondelete="cascade",
        ),
        ForeignKeyConstraint(
            [dag_run_id],
            ["dag_run.id"],
            name="bdr_dag_run_fkey",
            ondelete="set null",
        ),
    )

    @validates("sort_ordinal")
    def validate_sort_ordinal(self, key, val):
        if val < 1:
            raise ValueError("sort_ordinal must be >= 1")
        return val


def _get_latest_dag_run_row_query(info, session):
    from airflow.models import DagRun

    return (
        select(DagRun)
        .where(DagRun.logical_date == info.logical_date)
        .order_by(nulls_first(desc(DagRun.start_date), session=session))
        .limit(1)
    )


def _get_dag_run_no_create_reason(dr, reprocess_behavior: ReprocessBehavior) -> str | None:
    non_create_reason = None
    if dr.state not in (DagRunState.SUCCESS, DagRunState.FAILED):
        non_create_reason = BackfillDagRunExceptionReason.IN_FLIGHT
    elif reprocess_behavior is ReprocessBehavior.NONE:
        non_create_reason = BackfillDagRunExceptionReason.ALREADY_EXISTS
    elif reprocess_behavior is ReprocessBehavior.FAILED:
        if dr.state != DagRunState.FAILED:
            non_create_reason = BackfillDagRunExceptionReason.ALREADY_EXISTS
    return non_create_reason


def _validate_backfill_params(dag, reverse, reprocess_behavior: ReprocessBehavior | None):
    depends_on_past = None
    depends_on_past = any(x.depends_on_past for x in dag.tasks)
    if depends_on_past:
        if reverse is True:
            raise ValueError(
                "Backfill cannot be run in reverse when the dag has tasks where depends_on_past=True"
            )
        if reprocess_behavior in (None, ReprocessBehavior.NONE):
            raise ValueError(
                "Dag has task for which depends_on_past is true. "
                "You must set reprocess behavior to reprocess completed or "
                "reprocess failed"
            )


def _do_dry_run(*, dag_id, from_date, to_date, reverse, reprocess_behavior, session) -> list[datetime]:
    from airflow.models.serialized_dag import SerializedDagModel

    serdag = session.scalar(SerializedDagModel.latest_item_select_object(dag_id))
    dag = serdag.dag
    _validate_backfill_params(dag, reverse, reprocess_behavior)

    dagrun_info_list = _get_info_list(
        dag=dag,
        from_date=from_date,
        to_date=to_date,
        reverse=reverse,
    )
    logical_dates = []
    for info in dagrun_info_list:
        dr = session.scalar(
            statement=_get_latest_dag_run_row_query(info, session),
        )
        if dr:
            non_create_reason = _get_dag_run_no_create_reason(dr, reprocess_behavior)
            if not non_create_reason:
                logical_dates.append(info.logical_date)
        else:
            logical_dates.append(info.logical_date)
    return logical_dates


def _create_backfill_dag_run(
    *,
    dag: DAG,
    info: DagRunInfo,
    reprocess_behavior: ReprocessBehavior,
    backfill_id,
    dag_run_conf,
    backfill_sort_ordinal,
    session,
):
    with session.begin_nested() as nested:
        dr = session.scalar(
            with_row_locks(
                query=_get_latest_dag_run_row_query(info, session),
                session=session,
            ),
        )
        if dr:
            non_create_reason = _get_dag_run_no_create_reason(dr, reprocess_behavior)
            if non_create_reason:
                # rolling back here restores to start of this nested tran
                # which releases the lock on the latest dag run, since we
                # are not creating a new one
                nested.rollback()
                session.add(
                    BackfillDagRun(
                        backfill_id=backfill_id,
                        dag_run_id=None,
                        logical_date=info.logical_date,
                        exception_reason=non_create_reason,
                        sort_ordinal=backfill_sort_ordinal,
                    )
                )
                return
        dag_version = DagVersion.get_latest_version(dag.dag_id, session=session)
        dr = dag.create_dagrun(
            run_id=dag.timetable.generate_run_id(
                run_type=DagRunType.BACKFILL_JOB,
                logical_date=info.logical_date,
                data_interval=info.data_interval,
            ),
            logical_date=info.logical_date,
            data_interval=info.data_interval,
            conf=dag_run_conf,
            run_type=DagRunType.BACKFILL_JOB,
            triggered_by=DagRunTriggeredByType.BACKFILL,
            dag_version=dag_version,
            state=DagRunState.QUEUED,
            start_date=timezone.utcnow(),
            backfill_id=backfill_id,
            session=session,
        )
        session.add(
            BackfillDagRun(
                backfill_id=backfill_id,
                dag_run_id=dr.id,
                sort_ordinal=backfill_sort_ordinal,
                logical_date=info.logical_date,
            )
        )


def _get_info_list(
    *,
    from_date,
    to_date,
    reverse,
    dag,
):
    infos = dag.iter_dagrun_infos_between(from_date, to_date)
    now = timezone.utcnow()
    dagrun_info_list = (x for x in infos if x.data_interval.end < now)
    if reverse:
        dagrun_info_list = reversed([x for x in dag.iter_dagrun_infos_between(from_date, to_date)])
    return dagrun_info_list


def _create_backfill(
    *,
    dag_id: str,
    from_date: datetime,
    to_date: datetime,
    max_active_runs: int,
    reverse: bool,
    dag_run_conf: dict | None,
    reprocess_behavior: ReprocessBehavior | None = None,
) -> Backfill | None:
    from airflow.models import DagModel
    from airflow.models.serialized_dag import SerializedDagModel

    with create_session() as session:
        serdag = session.scalar(SerializedDagModel.latest_item_select_object(dag_id))
        if not serdag:
            raise NotFound(f"Could not find dag {dag_id}")
        # todo: if dag has no schedule, raise
        num_active = session.scalar(
            select(func.count()).where(
                Backfill.dag_id == dag_id,
                Backfill.completed_at.is_(None),
            )
        )
        if num_active > 0:
            raise AlreadyRunningBackfill(
                f"Another backfill is running for dag {dag_id}. "
                f"There can be only one running backfill per dag."
            )

        dag = serdag.dag
        _validate_backfill_params(dag, reverse, reprocess_behavior)

        br = Backfill(
            dag_id=dag_id,
            from_date=from_date,
            to_date=to_date,
            max_active_runs=max_active_runs,
            dag_run_conf=dag_run_conf,
            reprocess_behavior=reprocess_behavior,
        )
        session.add(br)
        session.commit()

        backfill_sort_ordinal = 0

        dagrun_info_list = _get_info_list(
            from_date=from_date,
            to_date=to_date,
            reverse=reverse,
            dag=dag,
        )

        log.info("obtaining lock on dag %s", dag_id)
        # we obtain a lock on dag model so that nothing else will create
        # dag runs at the same time. mainly this is required by non-uniqueness
        # of logical_date. otherwise we could just create run in a try-except.
        dag_model = session.scalar(
            with_row_locks(
                select(DagModel).where(DagModel.dag_id == dag_id),
                session=session,
            )
        )
        if not dag_model:
            raise RuntimeError(f"Dag {dag_id} not found")

        for info in dagrun_info_list:
            backfill_sort_ordinal += 1
            _create_backfill_dag_run(
                dag=dag,
                info=info,
                backfill_id=br.id,
                dag_run_conf=br.dag_run_conf,
                reprocess_behavior=br.reprocess_behavior,
                backfill_sort_ordinal=backfill_sort_ordinal,
                session=session,
            )
            log.info(
                "created backfill dag run dag_id=%s backfill_id=%s, info=%s",
                dag.dag_id,
                br.id,
                info,
            )
    return br
