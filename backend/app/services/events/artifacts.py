from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas.run import HarnessEvent, NormalizedRunDTO


@dataclass(frozen=True)
class ArtifactFingerprint:
    artifact_id: str
    path: str
    kind: str
    size: int | None
    mtime: datetime | None


def snapshot_artifacts(run: NormalizedRunDTO) -> dict[str, ArtifactFingerprint]:
    return {
        artifact.id: ArtifactFingerprint(
            artifact_id=artifact.id,
            path=artifact.path,
            kind=artifact.kind,
            size=artifact.size,
            mtime=artifact.mtime,
        )
        for artifact in run.artifacts
    }


def detect_artifact_changes(
    previous: dict[str, ArtifactFingerprint],
    current: dict[str, ArtifactFingerprint],
) -> list[HarnessEvent]:
    changed: list[HarnessEvent] = []
    sequence = 1

    for artifact_id, fingerprint in sorted(current.items()):
        previous_fingerprint = previous.get(artifact_id)
        if previous_fingerprint == fingerprint:
            continue

        changed.append(
            HarnessEvent(
                sequence=sequence,
                event_type="artifact_changed",
                ts=datetime.now(UTC),
                payload={
                    "artifact_id": artifact_id,
                    "path": fingerprint.path,
                    "kind": fingerprint.kind,
                    "change": "created" if previous_fingerprint is None else "modified",
                },
            )
        )
        sequence += 1

    return changed
