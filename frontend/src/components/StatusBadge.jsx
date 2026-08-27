import { Loader2, AlertTriangle, Clock } from "lucide-react";
import { STAGES } from "../constants.js";

export default function StatusBadge({ t }) {
  if (t.state === "draft") return <span className="ad-badge b-draft">Draft</span>;
  if (t.state === "failed") {
    return (
      <span className="ad-badge b-failed">
        <AlertTriangle aria-hidden="true" />
        Run failed
      </span>
    );
  }
  // Queued is deliberately not a spinner: nothing is happening to this report
  // yet. The position tells the moderator how many runs are ahead of theirs.
  if (t.state === "queued") {
    const pos = t.queuePosition;
    return (
      <span className="ad-badge b-queued">
        <Clock aria-hidden="true" />
        Queued{pos ? ` · ${pos} of ${t.queueLength}` : ""}
      </span>
    );
  }
  if (t.state === "inprocess") {
    const stage = STAGES.find((s) => s.k === t.stage);
    return (
      <span className="ad-badge b-inprocess">
        <Loader2 className="ad-spin" aria-hidden="true" />
        Running · {stage ? stage.label : "starting"}
      </span>
    );
  }
  if (t.state === "review") return <span className="ad-badge b-review">Awaiting review</span>;
  if (t.state === "new") return <span className="ad-badge b-review">In queue</span>;
  return <span className="ad-badge b-done">Done</span>;
}
