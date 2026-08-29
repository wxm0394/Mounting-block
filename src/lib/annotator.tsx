import React, { ReactNode } from 'react';

interface DifficultyInfo {
  base_level: number;
  adjusted_level: number;
}

interface AnnotationEntry {
  surface: string;
  lemma: string;
  kind: string;           // "word" | "phrase" | "entity"
  difficulty: DifficultyInfo;
  pos: string;
  syntax: { head: string; pattern: string } | null;
  semantic: { translation: string; translation_confidence: number; cognate_status: string; cognate_confidence: number } | null;
}

export const MOCK_DICTIONARY: Record<string, AnnotationEntry> = {};

// Match phrases and words from the dynamic dictionary against the text.
// Phrases (multi-word & hyphenated) are matched first by longest-match.
function findPhrasesAndWords(text: string, dict: Record<string, AnnotationEntry>): { text: string; isMatch: boolean; entry?: AnnotationEntry }[] {
  const allKeys = Object.keys(dict).sort((a, b) => b.length - a.length);
  const phraseKeys = allKeys.filter(k => k.includes(' ') || k.includes('-'));
  const wordKeys = new Set(allKeys.filter(k => !k.includes(' ') && !k.includes('-')));

  let remaining = text;
  const chunks: { text: string; isMatch: boolean; entry?: AnnotationEntry }[] = [];

  while (remaining.length > 0) {
    let matchedPhrase = false;

    for (const phrase of phraseKeys) {
      const lowerRemaining = remaining.toLowerCase();
      if (lowerRemaining.startsWith(phrase)) {
        const nextChar = remaining[phrase.length];
        if (!nextChar || !/[a-zA-Z]/.test(nextChar)) {
          chunks.push({ text: remaining.substring(0, phrase.length), isMatch: true, entry: dict[phrase] });
          remaining = remaining.substring(phrase.length);
          matchedPhrase = true;
          break;
        }
      }
    }

    if (matchedPhrase) continue;

    const hyphenMatch = remaining.match(/^([a-zA-Z]+-(?:[a-zA-Z]+-)*[a-zA-Z]+)/);
    if (hyphenMatch) {
      const compound = hyphenMatch[1];
      const lowerCompound = compound.toLowerCase();
      if (dict[lowerCompound]) {
        chunks.push({ text: compound, isMatch: true, entry: dict[lowerCompound] });
      } else {
        chunks.push({ text: compound, isMatch: false });
      }
      remaining = remaining.substring(compound.length);
      continue;
    }

    const wordMatch = remaining.match(/^([a-zA-Z]+)/);
    if (wordMatch) {
      const word = wordMatch[1];
      const lowerWord = word.toLowerCase();
      if (wordKeys.has(lowerWord) && dict[lowerWord]) {
        chunks.push({ text: word, isMatch: true, entry: dict[lowerWord] });
      } else {
        chunks.push({ text: word, isMatch: false });
      }
      remaining = remaining.substring(word.length);
    } else {
      const nonWordMatch = remaining.match(/^([^a-zA-Z]+)/);
      if (nonWordMatch) {
        chunks.push({ text: nonWordMatch[1], isMatch: false });
        remaining = remaining.substring(nonWordMatch[1].length);
      } else {
        chunks.push({ text: remaining[0], isMatch: false });
        remaining = remaining.substring(1);
      }
    }
  }

  return chunks;
}

export function annotateText(text: string, displayLevelThreshold: number, dict: Record<string, any>): ReactNode[] {
  // Filter dict to only include entries at or above the display level threshold
  const filteredDict: Record<string, AnnotationEntry> = {};
  for (const [key, entry] of Object.entries(dict)) {
    const dl = entry.difficulty?.adjusted_level ?? entry.difficulty?.display_level ?? entry.lexile ?? 0;
    if (dl >= displayLevelThreshold || entry.kind === 'entity' || entry.semantic?.cognate_status === 'false_friend') {
      filteredDict[key] = entry as AnnotationEntry;
    }
  }

  const chunks = findPhrasesAndWords(text, filteredDict);
  const annotationCounts: Record<string, number> = {};

  return chunks.map((chunk, index) => {
    if (chunk.isMatch && chunk.entry) {
      const key = chunk.text.toLowerCase();
      const count = annotationCounts[key] || 0;

      if (count >= 2) {
        return <React.Fragment key={index}>{chunk.text}</React.Fragment>;
      }

      annotationCounts[key] = count + 1;
      const entry = chunk.entry;
      const trans = entry.semantic?.translation ?? (entry as any).trans ?? '';
      const pos = entry.pos || '';
      const kind = entry.kind || (entry as any).type || 'word';

      let rtContent = trans;
      if (kind === 'word' && pos) {
        rtContent = `${trans} ${pos}`;
      }
      // Phrases and entities: just translation, no POS

      return (
        <ruby key={index} className={`annotation-${kind}`}>
          {chunk.text}
          <rt>{rtContent}</rt>
        </ruby>
      );
    }

    return <React.Fragment key={index}>{chunk.text}</React.Fragment>;
  });
}
