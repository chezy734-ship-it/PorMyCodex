import React, { useState } from 'react';
import { BookOpen, Sparkles, Volume2, Download, Loader2 } from 'lucide-react';

interface SentenceItem {
  original: string;
  vocalized: string;
  estimatedDurationSec?: number;
}

export default function App() {
  const [text, setText] = useState(
    'בראשית ברא אלהים את השמים ואת הארץ. והארץ היתה תהו ובהו וחשך על פני תהום.'
  );
  const [sentences, setSentences] = useState<SentenceItem[]>([]);
  const [explanation, setExplanation] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function handleVocalize() {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/book/vocalize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'שגיאה בניקוד');
      setSentences(Array.isArray(data.sentences) ? data.sentences : []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'שגיאה לא צפויה');
    } finally {
      setLoading(false);
    }
  }

  async function handleExplain() {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/book/explain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, mode: 'explain', bookTitle: 'PorMyCodex' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'שגיאה בביאור');
      setExplanation(data.result || '');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'שגיאה לא צפויה');
    } finally {
      setLoading(false);
    }
  }

  async function handleExportAudio() {
    setLoading(true);
    setError('');
    try {
      const chunkList = sentences.length > 0 ? sentences.map(s => s.vocalized) : [text];
      const res = await fetch('/api/book/export-audio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts: chunkList, voice: 'Kore' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'שגיאה בייצוא שמע');
      const link = document.createElement('a');
      link.href = `data:${data.mimeType || 'audio/wav'};base64,${data.audioBase64}`;
      link.download = 'pormycodex-audio.wav';
      link.click();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'שגיאה לא צפויה');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div dir="rtl" className="min-h-screen bg-slate-950 text-slate-100 p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        <header className="flex items-center justify-between border-b border-slate-800 pb-4">
          <div className="flex items-center gap-3">
            <BookOpen className="w-7 h-7 text-amber-400" />
            <div>
              <h1 className="text-xl font-bold text-slate-100">PorMyCodex</h1>
              <p className="text-sm text-slate-400">מקריא ספרים חכם בעברית</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={handleVocalize}
              disabled={loading}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-500 text-slate-950 font-semibold hover:bg-amber-400 disabled:opacity-50"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              נקד טקסט
            </button>
            <button
              onClick={handleExplain}
              disabled={loading}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-800 text-slate-100 hover:bg-slate-700 disabled:opacity-50"
            >
              <Volume2 className="w-4 h-4" />
              באר פסקה
            </button>
            <button
              onClick={handleExportAudio}
              disabled={loading}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-800 text-slate-100 hover:bg-slate-700 disabled:opacity-50"
            >
              <Download className="w-4 h-4" />
              ייצא שמע
            </button>
          </div>
        </header>

        {error && (
          <div className="rounded-lg bg-red-950/60 border border-red-700 px-4 py-3 text-red-200 text-sm">
            {error}
          </div>
        )}

        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          rows={6}
          className="w-full rounded-xl bg-slate-900 border border-slate-800 p-4 text-slate-100 leading-relaxed focus:outline-none focus:border-amber-500"
          placeholder="הזן טקסט בעברית..."
        />

        {sentences.length > 0 && (
          <section className="rounded-xl bg-slate-900 border border-slate-800 p-4 space-y-2">
            <h2 className="font-semibold text-amber-400">טקסט מנוקד</h2>
            {sentences.map((s, idx) => (
              <p key={idx} className="text-lg leading-loose text-slate-200">
                {s.vocalized}
              </p>
            ))}
          </section>
        )}

        {explanation && (
          <section className="rounded-xl bg-slate-900 border border-slate-800 p-4 space-y-2">
            <h2 className="font-semibold text-sky-400">ביאור והרחבה</h2>
            <div className="whitespace-pre-wrap text-slate-200 leading-relaxed">{explanation}</div>
          </section>
        )}
      </div>
    </div>
  );
}
