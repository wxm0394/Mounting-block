import React, { ReactNode } from 'react';

interface DifficultyInfo {
  base_level: number;
  adjusted_level: number;
}

export interface SubWordEntry {
  surface: string;
  lemma: string;
  level: number;
  pos: string;
  translation?: string;
  translation_status?: string;
}

export interface AnnotationEntry {
  surface: string;
  lemma: string;
  kind: string;           // "word" | "phrase" | "entity"
  difficulty: DifficultyInfo;
  pos: string;
  syntax: { head: string; pattern: string } | null;
  semantic: { translation: string; translation_confidence: number; cognate_status: string; cognate_confidence: number, translation_status: string } | null;
  sub_words?: SubWordEntry[];
}

export const MOCK_DICTIONARY: Record<string, AnnotationEntry> = {};

function composeRtContent(trans: string, pos: string, translationStatus: string | undefined, allowPos: boolean): React.ReactNode {
  if (trans) {
    return allowPos && pos ? `${trans} ${pos}` : trans;
  }
  if (translationStatus === "failed") {
    return "⚠️";
  }
  return "";
}

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

export function annotateText(text: string, dict: Record<string, any>, annotationCounts: Record<string, number> = {}): ReactNode[] {
  // Pass all dictionary entries to be matched. Display filtering is handled by CSS.
  // We still construct the full DOM structure for everything we matched.
  const chunks = findPhrasesAndWords(text, dict);

  return chunks.map((chunk, index) => {
    if (chunk.isMatch && chunk.entry) {
      const key = chunk.text.toLowerCase();
      const count = annotationCounts[key] || 0;

      // Limit to max 2 annotations per page
      if (count >= 2) {
        return <React.Fragment key={index}>{chunk.text}</React.Fragment>;
      }

      annotationCounts[key] = count + 1;
      const entry = chunk.entry;
      const trans = entry.semantic?.translation ?? (entry as any).trans ?? '';
      const kind = entry.kind || (entry as any).type || 'word';
      const rawLevel = entry.difficulty?.adjusted_level ?? entry.difficulty?.base_level ?? 500;
      const level = Math.round(rawLevel / 50) * 50;

      // Single word or entity (no subwords)
      if (kind === 'word' || kind === 'entity' || !entry.sub_words || entry.sub_words.length === 0) {
        const pos = entry.pos || '';
        const rtContent = composeRtContent(trans, pos, entry.semantic?.translation_status, kind === 'word');
        const isFailed = entry.semantic?.translation_status === 'failed';
        return (
          <ruby key={index} className={`annotation-${kind}`} data-level={level} data-translation-failed={isFailed ? "true" : undefined}>
            {chunk.text}
            <rt>{rtContent}</rt>
          </ruby>
        );
      }

      // Phrase with sub_words (use sibling views)
      let phraseRemainingText = chunk.text;
      const wordsViewNodes: ReactNode[] = [];

      for (const sw of entry.sub_words) {
        // Find the subword in the phrase's text (case insensitive) to retain original casing & punctuation
        const matchIdx = phraseRemainingText.toLowerCase().indexOf(sw.surface.toLowerCase());
        if (matchIdx !== -1) {
          // Push any preceding text/punctuation
          if (matchIdx > 0) {
            wordsViewNodes.push(<span key={`pre-${sw.surface}`}>{phraseRemainingText.substring(0, matchIdx)}</span>);
          }
          
          const actualText = phraseRemainingText.substring(matchIdx, matchIdx + sw.surface.length);
          const swRawLevel = sw.level ?? 500;
          const swLevel = Math.round(swRawLevel / 50) * 50;
          // Look up translation: either in subword itself or from the global dictionary
          const swTrans = sw.translation || dict[sw.lemma]?.semantic?.translation || dict[sw.surface]?.semantic?.translation || '';
          const isFailed = sw.translation_status === "failed";
          const swRtContent = composeRtContent(swTrans, sw.pos, sw.translation_status, true);

          wordsViewNodes.push(
            <ruby key={`word-${sw.surface}`} className="word-view annotation-word" data-level={swLevel} data-translation-failed={isFailed ? "true" : undefined}>
              {actualText}
              <rt>{swRtContent}</rt>
            </ruby>
          );
          
          phraseRemainingText = phraseRemainingText.substring(matchIdx + sw.surface.length);
        }
      }
      
      // Push any remaining trailing text
      if (phraseRemainingText.length > 0) {
        wordsViewNodes.push(<span key="post">{phraseRemainingText}</span>);
      }

      const phraseIsFailed = entry.semantic?.translation_status === "failed";
      const phraseRtContent = composeRtContent(trans, entry.pos, entry.semantic?.translation_status, false);

      return (
        <span key={index} className="annotation-unit" data-phrase-level={level}>
          <ruby className={`phrase-view annotation-${kind}`} data-level={level} data-translation-failed={phraseIsFailed ? "true" : undefined}>
            {chunk.text}
            <rt>{phraseRtContent}</rt>
          </ruby>
          <span className="words-view">
            {wordsViewNodes}
          </span>
        </span>
      );
    }

    return <React.Fragment key={index}>{chunk.text}</React.Fragment>;
  });
}

