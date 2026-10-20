from __future__ import annotations

from datetime import UTC, datetime

import pytest
from common.enums import JobStatus
from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile
from server.workspace import post_action_jobs, recovery_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [status.value for status in JobStatus])
@pytest.mark.parametrize("scoped", [False, True])
async def test_pending_action_scope(database_factory, status: str, scoped: bool) -> None:
    async with database_factory() as session:
        library = Library(name="queries", roots=["/library"])
        session.add(library)
        await session.flush()
        sources = [
            MediaFile(
                library_id=library.id,
                path=f"/library/{name}.mkv",
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="movie",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for name in ["owned", "idle"]
        ]
        session.add_all(sources)
        await session.flush()
        job = Job(media_file_id=sources[0].id, library_id=library.id, status=status, kind="healthcheck")
        idle = Job(media_file_id=sources[1].id, library_id=library.id, status="succeeded")
        session.add_all([job, idle])
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="succeeded")
        session.add(attempt)
        await session.flush()
        # A completion before the pending event, or for another job, cannot release locks.
        session.add(JobEvent(job_id=job.id, type="actions_finished"))
        await session.flush()
        session.add(JobEvent(job_id=job.id, type="actions_pending"))
        await session.flush()
        session.add_all(
            [
                JobEvent(job_id=idle.id, type="actions_finished"),
                JobEvent(job_id=job.id, attempt_id=attempt.id, type="actions_finished"),
            ]
        )
        await session.flush()

        source = aliased(MediaFile)
        protected = select(source.id).where(
            exists(recovery_jobs(include_artifact_checks=False, scoped=True).where(Job.media_file_id == source.id))
        )
        assert set(await session.scalars(post_action_jobs(scoped=scoped))) == {job.id}
        assert set(await session.scalars(recovery_jobs(scoped=scoped))) == {job.id}
        assert list(await session.scalars(protected)) == [sources[0].id]
        assert (
            await session.scalar(post_action_jobs(scoped=True).where(Job.media_file_id == sources[1].id).limit(1))
            is None
        )
        owner = aliased(Job)
        assert list(
            await session.scalars(
                select(Job.id).where(
                    exists(
                        select(owner.id).where(
                            owner.media_file_id == Job.media_file_id, owner.id.in_(post_action_jobs())
                        )
                    )
                )
            )
        ) == [job.id]

        session.add(JobEvent(job_id=job.id, type="actions_finished"))
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == []
        assert list(await session.scalars(protected)) == []

        session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type="actions_pending"))
        await session.flush()
        session.add(JobEvent(job_id=job.id, type="actions_finished"))
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == [job.id]
        session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type="actions_finished"))
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["declared", "stage", "post_actions"])
@pytest.mark.parametrize("attempt_present", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_retry_action_records(database_factory, event_type: str, attempt_present: bool, scoped: bool) -> None:
    async with database_factory() as session:
        library = Library(name="retry", roots=["/library"])
        session.add(library)
        await session.flush()
        sources = [
            MediaFile(
                library_id=library.id,
                path=f"/library/{name}.mkv",
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="movie",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for name in ["retry", "idle"]
        ]
        session.add_all(sources)
        await session.flush()
        job = Job(media_file_id=sources[0].id, library_id=library.id, status="pending", attempts=1)
        other = Job(media_file_id=sources[1].id, library_id=library.id, status="succeeded")
        session.add_all([job, other])
        await session.flush()
        attempt_id = None
        if attempt_present:
            attempt = JobAttempt(job_id=job.id, attempt_number=1, status="succeeded")
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id
        actions = [{"kind": "preserve_timestamps"}]
        if event_type == "declared":
            job.post_actions = actions
        else:
            session.add(
                JobEvent(
                    job_id=job.id,
                    attempt_id=attempt_id,
                    type=event_type,
                    payload={"post_actions" if event_type == "stage" else "actions": actions},
                )
            )
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == []
        session.add(
            JobEvent(
                job_id=job.id, type="requested", payload={"operation": "retry", "previous": {"status": "succeeded"}}
            )
        )
        await session.flush()
        session.add(JobEvent(job_id=other.id, type="requested", payload={"operation": "cancel"}))
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=True).where(Job.media_file_id == sources[0].id))) == [
            job.id
        ]
        assert list(await session.scalars(recovery_jobs(include_actions=False, scoped=scoped))) == []
        assert list(await session.scalars(recovery_jobs(scoped=scoped))) == [job.id]
        available = select(MediaFile.id).where(
            ~exists(recovery_jobs(include_artifact_checks=False, scoped=True).where(Job.media_file_id == MediaFile.id))
        )
        assert list(await session.scalars(available)) == [sources[1].id]

        session.add(
            JobEvent(job_id=job.id, type="requested", payload={"operation": "retry", "previous": {"status": "failed"}})
        )
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == []
        assert set(await session.scalars(available)) == {source.id for source in sources}
        job.status = "succeeded"
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == [job.id]
        session.add(JobEvent(job_id=job.id, attempt_id=attempt_id, type="actions_finished"))
        await session.flush()
        assert list(await session.scalars(post_action_jobs(scoped=scoped))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_recovery_record_scope(database_factory, scoped: bool) -> None:
    async with database_factory() as session:
        jobs = [Job(media_file_id=index, library_id=1, status="succeeded") for index in range(1, 9)]
        session.add_all(jobs)
        await session.flush()
        attempts = [
            JobAttempt(job_id=job.id, attempt_number=1, status="succeeded", workload="transcode") for job in jobs
        ]
        session.add_all(attempts)
        await session.flush()
        attempts[0].workload = "healthcheck"
        attempts[0].status = "running"
        attempts[1].workload = "healthcheck"
        attempts[1].log_path = "unread.log"
        attempts[2].workload = "healthcheck"
        attempts[2].result = "interrupted"
        attempts[2].log_data = b"saved"
        attempts[2].finished_at = datetime(2026, 1, 1, tzinfo=UTC)
        attempts[2].error = "requeued"
        session.add(
            FinalizationJournal(
                attempt_id=attempts[3].id,
                source_path="source",
                destination_path="destination",
                partial_path="partial",
                trash_path="trash",
                state="prepared",
            )
        )
        session.add(
            JobEvent(
                job_id=jobs[4].id,
                attempt_id=attempts[4].id,
                type="healthcheck_log_unavailable",
                payload={"log_path": "old.log"},
            )
        )
        session.add(JobEvent(job_id=jobs[5].id, type="healthcheck_log_recovered", payload={}))
        jobs[6].attempts = 1
        session.add(JobEvent(job_id=jobs[6].id, attempt_id=attempts[6].id, type="artifact_check_pending"))
        await session.flush()
        expected = {jobs[index].id for index in [0, 1, 3, 4, 5, 6]}
        assert set(await session.scalars(recovery_jobs(scoped=scoped))) == expected
        assert set(await session.scalars(recovery_jobs(include_artifact_checks=False, scoped=scoped))) == expected - {
            jobs[6].id
        }
        for job in jobs:
            assert await session.scalar(
                recovery_jobs(scoped=True).where(Job.media_file_id == job.media_file_id).limit(1)
            ) == (job.id if job.id in expected else None)

        attempts[0].status = "failed"
        attempts[1].log_data = b"saved"
        journal = await session.scalar(select(FinalizationJournal))
        assert journal is not None
        journal.state = "completed"
        attempts[4].log_path = "old.log"
        attempts[4].log_data = b"saved"
        session.add(
            JobEvent(
                job_id=jobs[4].id,
                attempt_id=attempts[4].id,
                type="healthcheck_log_recovered",
                payload={"removed": True, "log_path": "old.log"},
            )
        )
        # Recovery retains an earlier unremoved-log event even after another recovery event.
        session.add(JobEvent(job_id=jobs[5].id, type="healthcheck_log_recovered", payload={"removed": True}))
        session.add(Job(media_file_id=jobs[6].media_file_id, library_id=1, status="failed", kind="transcode"))
        await session.flush()
        assert set(await session.scalars(recovery_jobs(scoped=scoped))) == {jobs[5].id}
