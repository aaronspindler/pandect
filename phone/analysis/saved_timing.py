"""Verify the exact input manifest of a large saved direct-message timing report."""

import json

from django.db import connection

from core.models import AnalysisReportIndex

INLINE_MEMBERS = 64


def timing_members(case, result):
    # All fields used here are covered by the evidence/people change triggers.
    # A database trigger clears validation and bumps the version if the saved
    # inputs change. Warm reads need neither the manifest nor its originals.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.evidence, idx.verified_messages
              FROM core_analysisrun r
              JOIN core_analysisresult ar ON ar.run_id=r.id AND ar.case_id=r.case_id
              JOIN core_analysisreportindex idx ON idx.result_id=ar.id
              JOIN cases_caseactivity a ON a.case_id=r.case_id
             WHERE ar.id=%s AND r.case_id=%s AND r.operation='comm_patterns'
            """,
            [result.pk, case.pk],
        )
        row = cursor.fetchone()
        if not row:
            return None
        revision, cache = row
        if isinstance(cache, str):
            cache = json.loads(cache)
        if cache.get("revision") == revision:
            return cache.get("sample")
        # Recheck every member together in PostgreSQL. Only four example keys
        # cross the connection. No message body is read or transferred. A
        # concurrent change invalidates this stamp on the next read.
        cursor.execute(
            """
            SELECT count(*) = count(m.id) AND count(DISTINCT m.id) = count(*)
                   AND bool_and((
                        s->>'kind'='text-message' AND s->>'sha256'=m.sha256
                        AND s->>'attachment_id' IS NULL AND s->>'result_id' IS NULL
                        AND s->>'geocode_candidate' IS NULL AND s->>'review_id' IS NULL
                        AND sender.case_id=r.case_id AND dest.case_id=r.case_id
                   ) IS TRUE),
                   array_agg(m.id ORDER BY m.id) FILTER (WHERE pos <= 4), count(*)
              FROM core_analysisrun r
              CROSS JOIN LATERAL jsonb_array_elements(r.inputs)
                                  WITH ORDINALITY AS snapshot(s,pos)
              LEFT JOIN phone_textmessageevidence m
                     ON m.id=CASE WHEN pg_input_is_valid(s->>'evidence_id','uuid')
                                  THEN (s->>'evidence_id')::uuid END
                    AND m.case_id=r.case_id AND NOT m.is_archived
                    AND m.group_source_id IS NULL
              LEFT JOIN people_person sender ON sender.id=m.sender_id
              LEFT JOIN people_person dest ON dest.id=m.recipient_id
             WHERE r.id=%s AND r.case_id=%s
            """,
            [result.run_id, case.pk],
        )
        valid, ids, count = cursor.fetchone()
        sample = sorted(map(str, ids)) if valid else None

    checked = {
        "version": cache.get("version", 0),
        "revision": revision,
        "count": count,
        "sample": sample,
    }
    index = AnalysisReportIndex.objects.filter(result=result, result__case=case)
    if not index.filter(verified_messages=cache).update(verified_messages=checked):
        # Another validator or a changed manifest won the race.
        if not index.filter(verified_messages=checked).exists():
            return None
    return sample
