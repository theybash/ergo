/* The sample diff the chat's Diff rows draw. */

/* Kept for Diff.jsx, which the chat screen still wants. Insights does not draw a diff any more:
   the daemon records no last change, so there was nothing real to put in it. */
export const DIFF = {
  file: 'src/broker.py',
  added: 4,
  removed: 3,
  // kind is hunk, context, added or removed. gut is the line number, empty on the hunk row.
  rows: [
    { kind: 'hunk', gut: '', src: '@@ -114,9 +114,10 @@ def finish(job_id, result):' },
    { kind: 'context', gut: '114', src: '     path = REPLIES / f"{job_id}.json"' },
    { kind: 'removed', gut: '115', src: '-    if path.exists():' },
    { kind: 'removed', gut: '116', src: '-        path.unlink()' },
    { kind: 'removed', gut: '117', src: '-    write(path, result)' },
    { kind: 'added', gut: '115', src: '+    if path.exists():' },
    { kind: 'added', gut: '116', src: '+        return          # already written, do nothing' },
    { kind: 'added', gut: '117', src: '+    write(path, result)' },
    { kind: 'added', gut: '118', src: '+    log.info("reply written once", job_id)' },
    { kind: 'context', gut: '119', src: '     return path' },
  ],
}
