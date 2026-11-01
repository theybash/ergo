/* One service. The mark is a mono initial in a rounded square, the badge says where it stands,
   and the foot holds the one thing you can do with it.

   `live` is what the daemon measured: state, badge and scope. A card the daemon cannot check gets
   "not measured" and no scope, never a written down "connected". */

import Badge from '../../primitives/Badge'

export default function IntegrationCard({ item, live }) {
  const connected = live.badge === 'ok'
  const button = 'btn sm' + (connected ? '' : ' quiet')

  return (
    <article className="int-card">
      <div className="int-top">
        <span className="int-mark" aria-hidden="true">{item.mark}</span>
        <h3 className="int-name">{item.name}</h3>
        <Badge kind={live.badge}>{live.state}</Badge>
      </div>
      <p className="int-use">{item.use}</p>
      <p className="int-scope">{live.scope}</p>
      <div className="int-foot">
        <button type="button" className={button}>{connected ? 'Manage' : 'Connect'}</button>
      </div>
    </article>
  )
}
