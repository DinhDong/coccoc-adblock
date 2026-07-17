import { userOf } from "../constants.js";

export function Avatar({ uk, size = 22 }) {
  const u = userOf(uk);
  if (!u) return null;
  return (
    <span
      className="ad-av"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.4), background: u.bg, color: u.ink }}
      title={u.name}
      aria-label={u.name}
    >
      {u.initials}
    </span>
  );
}

export function Person({ uk }) {
  const u = userOf(uk);
  if (!u) return <span className="ad-mute">—</span>;
  return (
    <span className="ad-person">
      <Avatar uk={uk} size={22} />
      {u.name}
    </span>
  );
}
