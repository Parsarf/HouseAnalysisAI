import { useEffect, useState, type FormEvent } from "react";
import { createOutreachDraft, listProperties, streamChat, type OutreachDraft, type PropertyListItem } from "../api";
import { MoneyText } from "../components/Money";
import { parseScore, ScoreBar } from "../components/ScoreBar";

type Message = { role: "user" | "assistant"; content: string };

function ChatContent({ text }: { text: string }) {
  const parts = text.split(/(\$[\d,]+(?:\.\d+)?)/g);
  return <p>{parts.map((part, index) => part.startsWith("$")
    ? <MoneyText key={index} money={{ value: part.slice(1).replace(/,/g, ""), confidence: 1, source_kind: "derived", is_estimated: false }} />
    : part)}</p>;
}

function requestedOffer(text: string): string | undefined {
  const compact = text.replace(/,/g, "");
  const abbreviated = compact.match(/\$?\s*(\d+(?:\.\d+)?)\s*([km])\b/i);
  if (abbreviated) {
    const multiplier = abbreviated[2].toLowerCase() === "m" ? 1_000_000 : 1_000;
    return String(Math.round(Number(abbreviated[1]) * multiplier));
  }
  const dollars = compact.match(/\$\s*(\d{5,})\b/);
  return dollars?.[1];
}

export function ChatPage() {
  const initial = new URLSearchParams(window.location.search).get("property");
  const [properties, setProperties] = useState<PropertyListItem[]>([]);
  const [selected, setSelected] = useState<string[]>(initial ? [initial] : []);
  const [messages, setMessages] = useState<Message[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chatDraft, setChatDraft] = useState<OutreachDraft | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { listProperties({ limit: 100 }).then((result) => setProperties(result.items)).catch((reason: Error) => setError(reason.message)); }, []);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!input.trim() || busy) return;
    const next = [...messages, { role: "user" as const, content: input.trim() }];
    setMessages([...next, { role: "assistant", content: "" }]); setInput(""); setBusy(true); setError(null);
    try {
      const startsDraft = /\bdraft\b.*\b(email|outreach)\b/i.test(input);
      const revisesDraft = chatDraft && /\b(shorter|longer|formal|casual|raise|lower|change|revise|rewrite|make it|offer)\b/i.test(input);
      if ((startsDraft || revisesDraft) && selected.length === 1) {
        const offerPrice = requestedOffer(input) ?? chatDraft?.offer_price;
        const draft = await createOutreachDraft(selected[0], chatDraft ? {
          offer_price: offerPrice,
          prior_draft: { subject: chatDraft.subject, body: chatDraft.body },
          instruction: input.trim(),
        } : offerPrice ? { offer_price: offerPrice } : {});
        setChatDraft(draft);
        setMessages([...next, { role: "assistant", content: `Draft saved to the property workspace.\n\nSubject: ${draft.subject}\n\n${draft.body}\n\nReview the recipient candidates and edit the draft on the deal page before opening Gmail.` }]);
      } else {
        const returned = await streamChat(next, selected, (delta) => setMessages((current) => {
          const copy = [...current]; const last = copy[copy.length - 1]; copy[copy.length - 1] = { ...last, content: last.content + delta }; return copy;
        }), sessionId);
        setSessionId(returned);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Chat failed"); }
    finally { setBusy(false); }
  };
  return <section className="chat-page">
    <div className="page-header"><div><span className="eyebrow">Grounded analysis</span><h1>Property chat</h1><p>Answers use calculated records and source documents. ACQ does not recalculate figures in chat.</p></div></div>
    <section className="panel chat-selector"><strong>Properties</strong><div className="chat-chips">{properties.map((property) => <button key={property.id} className={selected.includes(property.id) ? "active" : ""} onClick={() => setSelected((current) => current.includes(property.id) ? current.filter((id) => id !== property.id) : [...current, property.id])}><span>{property.address_line1 ?? property.id}</span>{property.overall_score != null && <ScoreBar value={parseScore(property.overall_score) ?? 0} />}</button>)}</div><small>{selected.length === 0 ? "Portfolio-wide mode" : `${selected.length} selected`}</small></section>
    <section className="panel chat-thread">{messages.length === 0 ? <div className="empty-state compact"><strong>Ask about a deal or compare properties</strong><span>Figures are cited from structured analysis or source pages.</span></div> : messages.map((message, index) => <article key={index} className={`chat-message ${message.role}`}><strong>{message.role === "user" ? "You" : "ACQ"}</strong><ChatContent text={message.content || "Thinking…"} /></article>)}</section>
    {error && <div className="inline-error">{error}</div>}
    <form className="panel chat-compose" onSubmit={submit}><textarea className="text-area" rows={3} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Which selected property has the strongest expected equity, and why?" /><button className="btn btn-primary" disabled={busy || !input.trim()}>{busy ? "Analyzing…" : "Send"}</button></form>
  </section>;
}
