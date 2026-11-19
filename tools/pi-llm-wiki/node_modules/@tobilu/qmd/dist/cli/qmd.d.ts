import { type OutputFormat } from "./formatter.js";
type CliLifecycleWritable = {
    write(chunk: string | Uint8Array, callback?: (error?: Error | null) => void): boolean;
};
type FinishSuccessfulCliCommandOptions = {
    command: string;
    format?: OutputFormat;
    cleanup?: () => Promise<void>;
    exit?: (code: number) => void;
    stdout?: CliLifecycleWritable;
    stderr?: CliLifecycleWritable;
};
/**
 * Finish a successful CLI command after output has been flushed.
 *
 * We deliberately do NOT call `process.exit(0)`. `process.exit()` skips
 * Node's `beforeExit` event, and node-llama-cpp registers a `beforeExit` hook
 * that auto-disposes its native handles. On darwin, without that hook firing,
 * libggml-metal's static `ggml_metal_device` destructor asserts on a
 * non-empty residency-set collection during `__cxa_finalize_ranges` and
 * dumps a multi-kB backtrace (upstream ggml-org/llama.cpp#22593, fix open as
 * PR #22595). Empirically, even with explicit `disposeDefaultLlamaCpp()` the
 * direct `process.exit(0)` path still trips the assertion — letting the
 * event loop drain naturally is what actually clears the rsets.
 *
 * So: set `process.exitCode = 0` and return. The main module finishes, the
 * event loop drains, `beforeExit` fires, native resources tear down in
 * order, and the process exits cleanly. The `GGML_METAL_NO_RESIDENCY=1` env
 * var that `bin/qmd` exports is a defense-in-depth safety net for paths
 * that still call `process.exit()` after loading the native binding
 * (signal handlers, error paths, `bun test`).
 *
 * If the caller passes an explicit `exit` for testability, we honor it —
 * the lifecycle tests verify the legacy flush → cleanup → exit ordering.
 * Production callers must not pass `exit`.
 */
export declare function finishSuccessfulCliCommand(options: FinishSuccessfulCliCommandOptions): Promise<void>;
export declare function resolveEmbedModelForCli(): string;
export declare function resolveGenerateModelForCli(): string;
export declare function resolveRerankModelForCli(): string;
export declare function buildEditorUri(template: string, absolutePath: string, line: number, col: number): string;
export declare function termLink(text: string, url: string, isTTY?: boolean): string;
export {};
