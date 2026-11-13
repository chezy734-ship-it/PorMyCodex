import express, { Request, Response } from 'express';
import { createServer as createViteServer } from 'vite';
import { GoogleGenAI } from '@google/genai';
import path from 'path';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = 3000;
const app = express();

app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ extended: true, limit: '50mb' }));

// Initialize Gemini Client
const geminiApiKey = process.env.GEMINI_API_KEY;
let aiClient: GoogleGenAI | null = null;
if (geminiApiKey) {
  aiClient = new GoogleGenAI({
    apiKey: geminiApiKey,
    httpOptions: {
      headers: {
        'User-Agent': 'aistudio-build',
      },
    },
  });
}

// 1. Hebrew Diacritization (Nikud) & Acronym Expansion
app.post('/api/book/vocalize', async (req: Request, res: Response) => {
  try {
    const { text, options } = req.body;
    if (!text || typeof text !== 'string') {
      res.status(400).json({ error: 'Text is required for vocalization' });
      return;
    }

    if (!aiClient) {
      const sentences = text
        .split(/(?<=[.?!;:\n])\s+/)
        .map(s => s.trim())
        .filter(Boolean);
      res.json({
        vocalizedText: text,
        sentences: sentences.map(s => ({ original: s, vocalized: s })),
        source: 'fallback-no-key',
      });
      return;
    }

    const expandAcronyms = options?.expandAcronyms !== false;
    const addNikud = options?.addNikud !== false;
    const simplify = options?.simplify === true;

    const prompt = `
אתה מומחה בלשנות עברית, פונטיקה והכנת טקסטים לקריאה קולית רציפה (TTS).
עבור הטקסט הבא, בצע הכנה מושלמת לקריאה שוטפת ומדויקת:
1. ${addNikud ? 'הוסף ניקוד מלא ומדויק לכל המילים (כולל דגשים, תנועות והבחנה בין שׁ ימנית לשׂ שמאלית), כדי שמנוע דיבור יקריא ללא שיבושים.' : 'שמור על הטקסט ללא ניקוד אך תקן שגיאות פיסוק.'}
2. ${expandAcronyms ? 'פתח ראשי תיבות וקיצורים בעברית למילים מלאות ומנוקדות (למשל: רמב"ם -> רַבֵּינוּ מֹשֶׁה בֶּן מַיְמוֹן, עכ"פ -> עַל כָּל פָּנִים, אחה"צ -> אַחַר הַצָּהֳרַיִם, וכד\' -> וְכַדּוֹמֶה, ביהמ"ש -> בֵּית הַמִּשְׁפָּט, זצ"ל -> זֵכֶר צַדִּיק לִבְרָכָה, וכדומה).' : 'השאר קיצורים כפי שהם.'}
3. נקה סימוני שוליים מפריעים כגון מספרי הערות שוליים בסוגריים ([1], (א), [ע"ש]).
4. חלק את הטקסט למשפטים ברורים וקולחים המותאמים לקצב נשימה בקריאה.
${simplify ? '5. פשט קטעים ארכאיים או מפותלים לשפה עברית מודרנית ובהירה.' : ''}

טקסט המקור:
"""
${text.slice(0, 8000)}
"""

החזר תשובה אך ורק במבנה JSON תקני:
{
  "vocalizedText": "הטקסט המלא, מנוקד ורציף לקריאה",
  "sentences": [
    {
      "original": "משפט מקורי",
      "vocalized": "מִשְׁפָּט מְנֻקָּד",
      "estimatedDurationSec": 3.5
    }
  ],
  "glossary": [
    { "term": "מילה או מונח קשה", "meaning": "הסבר תמציתי בעברית בהירה" }
  ]
}
`;

    const aiRes = await aiClient.models.generateContent({
      model: 'gemini-3.8-flash',
      contents: prompt,
      config: {
        responseMimeType: 'application/json',
      },
    });

    const parsed = JSON.parse(aiRes.text || '{}');
    res.json({
      vocalizedText: parsed.vocalizedText || text,
      sentences: Array.isArray(parsed.sentences) && parsed.sentences.length > 0
        ? parsed.sentences
        : [{ original: text, vocalized: parsed.vocalizedText || text, estimatedDurationSec: 5 }],
      glossary: parsed.glossary || [],
      source: 'gemini-3.8-flash',
    });
  } catch (err: any) {
    console.error('Error vocalizing text:', err);
    res.status(500).json({ error: err.message || 'Failed to vocalize Hebrew text' });
  }
});

// 2. High-Fidelity Neural TTS via Gemini 3.8 Flash Lite TTS
app.post('/api/book/tts', async (req: Request, res: Response) => {
  try {
    const { text, voice, style } = req.body;
    if (!text || typeof text !== 'string') {
      res.status(400).json({ error: 'Text is required for TTS' });
      return;
    }

    if (!aiClient) {
      res.status(400).json({
        error: 'Gemini API Key is not configured on the server. Please use browser speech synthesis fallback.',
        useBrowserSpeech: true,
      });
      return;
    }

    const allowedVoices = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Zephyr'];
    const chosenVoice = allowedVoices.includes(voice) ? voice : 'Kore';

    const cleanText = text.slice(0, 1500).trim();
    if (!cleanText) {
      res.status(400).json({ error: 'Empty text chunk' });
      return;
    }

    const ttsResponse = await aiClient.models.generateContent({
      model: 'gemini-3.8-flash-lite-tts',
      contents: [
        {
          role: 'user',
          parts: [
            {
              text: cleanText,
              speechMetadata: {
                style: style || 'Clear, natural, warm Hebrew reading cadence, expressive and articulate',
              },
            },
          ],
        },
      ],
      config: {
        responseModalities: ['AUDIO'],
        speechConfig: {
          voiceConfig: {
            prebuiltVoiceConfig: { voiceName: chosenVoice },
          },
        },
      },
    });

    const base64Audio = ttsResponse.candidates?.[0]?.content?.parts?.[0]?.inlineData?.data;
    if (!base64Audio) {
      throw new Error('No audio data received from Gemini TTS model');
    }

    res.json({
      audioBase64: base64Audio,
      mimeType: 'audio/wav',
      voice: chosenVoice,
    });
  } catch (err: any) {
    console.error('Gemini TTS error:', err);
    res.status(500).json({
      error: err.message || 'Failed to generate audio via Gemini TTS',
      useBrowserSpeech: true,
    });
  }
});

// 3. AI Book Companion (Summary, Concept Explanation, Quiz, Simplification)
app.post('/api/book/explain', async (req: Request, res: Response) => {
  try {
    const { text, bookTitle, mode } = req.body;
    if (!text) {
      res.status(400).json({ error: 'Text is required' });
      return;
    }

    if (!aiClient) {
      res.status(400).json({ error: 'Gemini API is not configured' });
      return;
    }

    let instruction = 'באר את הטקסט בעברית בהירה, הסבר מילים קשות, הקשר היסטורי ורעיונות מרכזיים.';
    if (mode === 'summary') {
      instruction = 'כתוב סיכום מתומצת ומאיר עיניים של הקטע בנקודות (Bullet points), עם מסקנות ורעיונות מרכזיים.';
    } else if (mode === 'quiz') {
      instruction = 'חבר 3 שאלות הבנה מעניינות עם תשובות קצרות לבדיקת הבנת הנקרא.';
    } else if (mode === 'simplify') {
      instruction = 'נסח את כל הקטע מחדש בשפה עברית יפה, קולחת ומודרנית, כך שיהיה פשוט ונעים לקריאה לכל אדם.';
    }

    const prompt = `
אתה עוזר ספרותי אישי ומקצועי למקריא הספרים PorMyCodex.
${bookTitle ? `שם הספר / יצירה: "${bookTitle}"` : ''}

משימה:
${instruction}

הטקסט לניתוח:
"""
${text.slice(0, 6000)}
"""

ענה בעברית עשירה ובהירה בפורמט Markdown קריא ומסודר.
`;

    const aiRes = await aiClient.models.generateContent({
      model: 'gemini-3.8-flash',
      contents: prompt,
    });

    res.json({
      result: aiRes.text || 'לא התקבלה תשובה מהמודל',
      mode: mode || 'explain',
    });
  } catch (err: any) {
    console.error('Book explanation error:', err);
    res.status(500).json({ error: err.message || 'Failed to analyze text' });
  }
});

// 4. Batch Audio Stitching / Downloadable Audiobook
app.post('/api/book/export-audio', async (req: Request, res: Response) => {
  try {
    const { texts, voice } = req.body;
    if (!Array.isArray(texts) || texts.length === 0) {
      res.status(400).json({ error: 'Texts array is required' });
      return;
    }

    if (!aiClient) {
      res.status(400).json({ error: 'Gemini API not available for audio export' });
      return;
    }

    const chosenVoice = voice || 'Kore';
    const pcmChunks: Buffer[] = [];
    let headerBuffer: Buffer | null = null;

    const chunksToProcess = texts.slice(0, 10);

    for (const chunkText of chunksToProcess) {
      const clean = chunkText.slice(0, 800).trim();
      if (!clean) continue;

      const ttsRes = await aiClient.models.generateContent({
        model: 'gemini-3.8-flash-lite-tts',
        contents: [
          {
            role: 'user',
            parts: [{ text: clean }],
          },
        ],
        config: {
          responseModalities: ['AUDIO'],
          speechConfig: {
            voiceConfig: {
              prebuiltVoiceConfig: { voiceName: chosenVoice },
            },
          },
        },
      });

      const b64 = ttsRes.candidates?.[0]?.content?.parts?.[0]?.inlineData?.data;
      if (b64) {
        const rawWav = Buffer.from(b64, 'base64');
        if (rawWav.length >= 44) {
          if (!headerBuffer) {
            headerBuffer = Buffer.from(rawWav.subarray(0, 44));
          }
          pcmChunks.push(rawWav.subarray(44));
        }
      }
    }

    if (!headerBuffer || pcmChunks.length === 0) {
      res.status(500).json({ error: 'No audio chunks could be generated' });
      return;
    }

    const fullPcm = Buffer.concat(pcmChunks);
    const finalWav = Buffer.alloc(44 + fullPcm.length);
    headerBuffer.copy(finalWav, 0, 0, 44);
    fullPcm.copy(finalWav, 44);

    finalWav.writeUInt32LE(fullPcm.length + 36, 4);
    finalWav.writeUInt32LE(fullPcm.length, 40);

    const fullBase64 = finalWav.toString('base64');
    res.json({
      audioBase64: fullBase64,
      mimeType: 'audio/wav',
      sizeBytes: finalWav.length,
      chunksProcessed: chunksToProcess.length,
    });
  } catch (err: any) {
    console.error('Audio export error:', err);
    res.status(500).json({ error: err.message || 'Failed to export audio' });
  }
});

// Vite Middleware
async function startServer() {
  const isProduction = process.env.NODE_ENV === 'production';
  if (!isProduction) {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    app.use(express.static(path.resolve(__dirname, 'dist')));
    app.get('*', (req, res) => {
      res.sendFile(path.resolve(__dirname, 'dist', 'index.html'));
    });
  }

  app.listen(PORT, () => {
    console.log(`PorMyCodex server running on port ${PORT}`);
  });
}

startServer();
