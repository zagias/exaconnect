import { useEffect, useRef, useState } from "react";

/*
 * Speech input for the voice change box and the assistant (ADR 0039): the
 * browser's own speech recognition, where the browser has it. Typing always
 * works; the button only appears when speech is available, and what was
 * heard goes into the text box for the person to check before anything is
 * proposed. Nothing is sent until they press the box's own button.
 */

interface Recognition {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  continuous: boolean;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function speechAvailable(): boolean {
  return typeof window !== "undefined" && recognitionCtor() !== null;
}

const ERRORS: Record<string, string> = {
  "not-allowed": "The browser wasn't allowed to use the microphone. Type instead, or allow it in the browser's settings.",
  "service-not-allowed": "Speech isn't allowed here. Type instead.",
  "no-speech": "Nothing was heard. Try again, or type.",
  network: "The browser's speech service couldn't be reached. Type instead.",
  "audio-capture": "No microphone was found. Type instead.",
};

/** A "Speak" button that fills `onText` with what was heard. Renders nothing without browser speech. */
export function SpeechButton({ onText, lang = "en-GB", label = "Speak instead of typing" }: { onText: (t: string) => void; lang?: string; label?: string }) {
  const [listening, setListening] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const [available] = useState(speechAvailable);
  useEffect(() => () => rec.current?.abort(), []);
  if (!available) return null;
  const start = () => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const r = new Ctor();
    r.lang = lang;
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.continuous = false;
    r.onresult = (e) => {
      const text = Array.from(e.results)
        .map((res) => res[0]?.transcript ?? "")
        .join(" ")
        .trim();
      if (text) onText(text);
    };
    r.onerror = (e) => setNote(ERRORS[e.error] ?? "Speech stopped. Type instead.");
    r.onend = () => setListening(false);
    rec.current = r;
    setNote(null);
    setListening(true);
    try {
      r.start();
    } catch {
      setListening(false);
      setNote("Speech couldn't start. Type instead.");
    }
  };
  const stop = () => rec.current?.stop();
  return (
    <>
      <button type="button" className={`button secondary${listening ? " listening" : ""}`} onClick={listening ? stop : start} aria-pressed={listening} aria-label={listening ? "Stop listening" : label}>
        {listening ? "Stop" : "Speak"}
      </button>
      <span className="sr-only" role="status" aria-live="polite">
        {listening ? "Listening" : ""}
      </span>
      {note && (
        <span className="small muted" role="alert">
          {note}
        </span>
      )}
    </>
  );
}
