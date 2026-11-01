/* The rows of a unified diff, and nothing around them. The Insights screen wraps these in a
   framed .diff, the chat wraps them in a folded .edit, so the rows themselves live here once. */

// The mock marks an added row .add and a removed row .del.
const ROW_CLASS = { hunk: 'diff-row hunk', context: 'diff-row', added: 'diff-row add', removed: 'diff-row del' }

export default function DiffRows({ rows }) {
  return (
    <div className="diff-body">
      {rows.map(function (row, i) {
        return (
          <div className={ROW_CLASS[row.kind]} key={i}>
            <span className="gut">{row.gut}</span>
            <span className="src">{row.src}</span>
          </div>
        )
      })}
    </div>
  )
}
