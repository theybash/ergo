/* A square button that holds an icon and nothing else, so the label is the only name
   a screen reader gets. Pass the SVG as the children. */

export default function IconButton({ label, children, className, onClick, ...rest }) {
  return (
    <button
      type="button"
      className={className ? 'iconbtn ' + className : 'iconbtn'}
      aria-label={label}
      onClick={onClick}
      {...rest}
    >
      {children}
    </button>
  )
}
