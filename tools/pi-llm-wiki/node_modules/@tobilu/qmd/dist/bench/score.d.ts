/**
 * Scoring functions for the QMD benchmark harness.
 *
 * Computes precision@k, recall, MRR, and F1 for search results
 * against ground-truth expected files.
 */
/**
 * Normalize a file path for comparison.
 * Strips qmd:// prefix, lowercases, removes leading/trailing slashes.
 */
export declare function normalizePath(p: string): string;
/**
 * Check if two paths refer to the same file.
 * Handles different path formats by comparing normalized suffixes.
 */
export declare function pathsMatch(result: string, expected: string): boolean;
type ScoreMetrics = {
    precision_at_k: number;
    recall: number;
    recall_at_1: number;
    recall_at_3: number;
    recall_at_5: number;
    mrr: number;
    f1: number;
    hits_at_k: number;
    matched_files: string[];
    unmatched_expected_files: string[];
};
/**
 * Score a set of search results against expected files.
 */
export declare function scoreResults(resultFiles: string[], expectedFiles: string[], topK: number): ScoreMetrics;
export {};
