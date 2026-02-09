from uuid import UUID, uuid4
from app.db import get_pool

class ArtifactsRepo:
    async def create(
        self,
        *,
        user_id: UUID,
        project_id: UUID | None,
        job_id: UUID | None,
        kind: str,
        storage_path: str,
        content_type: str,
        bytes: int,
        sha256: str,
    ) -> UUID:
        aid = uuid4()
        pool = await get_pool()
        await pool.execute(
            """
            insert into music_artifacts(id, user_id, project_id, job_id, kind, storage_path, content_type, bytes, sha256)
            values($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            aid, user_id, project_id, job_id, kind, storage_path, content_type, bytes, sha256
        )
        return aid

    async def get(self, artifact_id: UUID) -> dict | None:
        pool = await get_pool()
        row = await pool.fetchrow("select * from music_artifacts where id=$1", artifact_id)
        return dict(row) if row else None