# Recover a stalled recording handoff

`velcro import` and `velcro recordings run` are separate processes. Importing a
recording does not start transcription: a processing machine must see the same
folder, choose an engine and run the processing command on a timer. Installing
the Mac app alone does not install that processing timer.

## Locate the break

On the import machine, check that the recorder was recognized, the offline queue
was flushed, and the verified file and its `.velcro-import.jsonl` entry reached
the destination. On the processing machine, run:

```sh
velcro recordings
velcro recordings check --require tidy --require deliver --require done
```

Remove a `--require` only for an intentionally unused hook. `check` is read-only:
it prints JSON, runs no engines or hooks, and returns 0 for complete, 1 for gaps,
or 2 for invalid arguments or an unavailable directory. It checks:

- A manifest exists and its lines can be read as recording records.
- Audio files with `import.ext` extensions in that folder have manifest entries.
  Registered older files excluded by `recordings.since` are not treated as orphans.
- Every selected recording has its source, a matching transcript receipt and a
  nonempty regular transcript file, and every configured per-file step completed.
- Required hooks are configured; when `done` is required, selected older records
  without an import id are reported instead of silently omitting their handoff.
- Every import's current set of recordings has a successful done hook receipt.

A source intentionally archived elsewhere remains `source gone`; record that
exception in your own monitoring policy. The check does not hash audio again,
inspect transcript content, or check a recorder/queue on another machine. Empty
valid manifests are healthy when no work is expected. Monitor expected recorder
arrivals and check-timer freshness independently, so a stopped importer or a
stopped daily check cannot turn into a healthy zero count.

## Recover delivery

Fix the unavailable mount, missing configuration, engine or receiver first, then
run `velcro recordings run`. Failed steps keep their retry/backoff schedule;
do not delete transcript receipts or the state directory to bypass it. Existing
transcription is preserved when a later hook fails.

For new imports, use a `recordings.done` command when the receiver needs a whole
group. A late queue flush can add a file to an import after its earlier files
were handed on. The done hook runs again for that changed group, with all its
records and a new `VELCRO_IMPORT_REVISION`. The import id remains stable.

Receivers should deduplicate on import id plus revision, and deduplicate work
per recording/hash when updating an existing meeting. Timestamp-only completion
entries from the first done-hook implementation have no membership proof and
are replayed once after upgrading. An interrupted hook can also be retried:
delivery is at least once, so side effects must be idempotent.

## Run an independent daily reconciliation

Schedule `velcro recordings check --require tidy --require deliver --require done`
at 09:00 in your local time zone, separately from the processing timer. Capture
its JSON and exit status, alert on any nonzero status or missing fresh report,
and identify missing files/stages rather than comparing total recording and
note counts. A timer's successful process exit from `recordings run` is not
evidence of complete delivery; that command intentionally records failures and
returns 0 for the next retry.

If delivery merely enqueues work, the receiver must separately reconcile its
accepted recording ids against completed notes and question drafts. Exit 0
from a hook acknowledges only what that hook actually guarantees. The receiver
must return nonzero when it has not durably accepted work; a background process
started with `&` is not a durable acknowledgment.

For a full acceptance test, import one short synthetic recording with no private
conversation. Observe its manifest entry, actual engine transcript, generated
notes, durable receiver acknowledgment and the question draft in the intended
operator's queue. Run processing and reconciliation again and verify no duplicate
notes or questions. Fake engines and local fixture hooks test the hook contract;
they do not establish this production acceptance result.
