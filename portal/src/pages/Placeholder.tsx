import { Eyebrow } from "../components";

interface Props {
  title: string;
  milestone: string;
  about: string;
}

export default function Placeholder({ title, milestone, about }: Props) {
  return (
    <>
      <div className="page-head">
        <Eyebrow>{title}</Eyebrow>
        <h1>{title}</h1>
      </div>
      <section className="card">
        <p style={{ marginTop: 0 }}>{about}</p>
        <p className="muted" style={{ marginBottom: 0 }}>This screen arrives in {milestone}.</p>
      </section>
    </>
  );
}
