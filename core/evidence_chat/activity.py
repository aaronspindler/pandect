"""Report bounded worker milestones and resolve the evidence they refer to."""

from django.utils import timezone

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

from . import corpus

STEPS = {
    "question": ("Reading the question", "Read the question"),
    "search": ("Searching case evidence", "Searched case evidence"),
    "compare": ("Comparing matching excerpts", "Compared matching excerpts"),
    "follow_up": (
        "Searching for follow-up evidence",
        "Searched for follow-up evidence",
    ),
    "answer": ("Preparing an answer", "Worked on a reply"),
    "select": (
        "Selecting evidence for a full review",
        "Selected evidence for a full review",
    ),
    "review": ("Reviewing source entries", "Reviewed source entries"),
    "summary": ("Summarizing the matches", "Summarized the matches"),
    "verify": ("Verifying review coverage", "Verified review coverage"),
    "publish": ("Checking the reply and sources", "Checked the reply and sources"),
}
ACTIVE_LABELS = {"queued": "Queued", "running": "Processing question"}
SOURCE_LIMIT = 8
STEP_LIMIT = 12


def record(turn, step, windows=()):
    """Save an observed stage without advancing the execution checkpoint."""
    steps = turn.activity.get("steps", [])
    if not steps or steps[-1] != step:
        steps = [*steps, step][-STEP_LIMIT:]
    recent = {loc["record_key"]: loc for loc in turn.activity.get("sources", [])}
    for window in windows:
        locator = window.locator
        key = locator["record_key"]
        recent.pop(key, None)
        recent[key] = {k: v for k, v in locator.items() if k not in {"label", "gap"}}
    update = {"steps": steps, "sources": list(recent.values())[-SOURCE_LIMIT:]}
    if all(turn.activity.get(key) == value for key, value in update.items()):
        return
    update["revision"] = timezone.now().isoformat()
    changed = ChatTurn.objects.filter(
        pk=turn.pk,
        case_id=turn.case_id,
        chat_id=turn.chat_id,
        delivery_token=turn.delivery_token,
        status=ChatTurn.Status.RUNNING,
    ).update(activity=update)
    if not changed:
        raise AnalysisError("processing_cancelled")
    turn.activity = update


def keep_ledger_sample(turn):
    """Keep only sample entries already retained as sources of this reply."""
    if recent := turn.activity.pop("sources", []):
        keys = [locator["key"] for locator in reversed(recent)]
        retained = set(
            turn.sources.filter(case_id=turn.case_id, key__in=keys).values_list(
                "key", flat=True
            )
        )
        turn.activity["source_keys"] = [key for key in keys if key in retained]


def describe(turn):
    steps = [key for key in turn.activity.get("steps", []) if key in STEPS]
    label = ACTIVE_LABELS.get(turn.status)
    if not label and not steps:
        return None
    current = turn.status == ChatTurn.Status.RUNNING and bool(steps)
    entries = []
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        active = current and last
        # The final attempted step may have stopped before it finished.
        ongoing = active or (last and turn.status in ("failed", "queued"))
        present, past = STEPS[step]
        entries.append({"label": present if ongoing else past, "current": active})
    if current:
        label = entries[-1]["label"]
    elif turn.status == ChatTurn.Status.QUEUED:
        entries.append({"label": "Waiting to start", "current": True})
    elif not entries:
        entries.append({"label": label, "current": True})
    sources = []
    if not turn.include_archived:
        if keys := turn.activity.get("source_keys"):
            ledger = dict(
                turn.sources.filter(case_id=turn.case_id, key__in=keys).values_list(
                    "key", "locator"
                )
            )
            locators = (ledger[key] for key in keys if key in ledger)
        else:
            locators = reversed(turn.activity.get("sources", []))
        for locator in locators:
            if source := corpus.read_window(turn.case, locator, 0, 1, review=True):
                sources.append({"label": source["label"], "url": source["url"]})
    return {"label": label or "Review activity", "steps": entries, "sources": sources}
