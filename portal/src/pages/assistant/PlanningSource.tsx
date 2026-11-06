import type { DesignReview } from "../../api";
import type { PlanningBrief } from "./PlanningBrief";

export function PlanningSource({ brief, review }: { brief: PlanningBrief; review?: DesignReview | null }) {
  return <section className="planning-quote-source" aria-label="Original planning requirements">
    <h3>Original requirements</h3>
    <p className="small muted">{brief.offer} · Visitor-reviewed planning notes. These are not verified live configuration.</p>
    <details><summary>View all {brief.facts.length} notes</summary><dl>{brief.facts.map((fact, index) => <div key={index}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl></details>
    {brief.gaps.length > 0 && <><h4>Still to confirm</h4><ul>{brief.gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></>}
    {review?.required && <p className="planning-quote-review" role="status">{review.status === "approved" && review.approved_digest === review.current_digest
      ? "Design scope reviewed by XaKore."
      : "XaKore must review this scope before the quote can be finished or sent."}</p>}
    {review?.reviewed_at && review.status === "approved" && <p className="small muted">Reviewed {new Date(review.reviewed_at).toLocaleString("en-GB")}{review.reviewed_by ? ` by ${review.reviewed_by.replace(/^user:/, "")}` : ""}. {review.note}</p>}
  </section>;
}
