#!/usr/bin/env node

/**
 * migrate-llm-wiki.js
 *
 * One-time migration script for wiki vault layouts.
 *
 * Two migrations are supported:
 *
 * 1. LEGACY → NEW (default): moves an old-style vault (`.wiki/` sentinel)
 *    to the new `.llm-wiki/` layout.
 *
 * 2. DOUBLED → FLATTENED (`--fix-doubled`): flattens a vault that was
 *    accidentally created at `<root>/.llm-wiki/.llm-wiki/…` due to a bug in
 *    `getPersonalWikiRoot()` (fixed in 0.6.4). The extension auto-runs this
 *    migration on `session_start`, but the script is provided for manual
 *    recovery on arbitrary roots.
 *
 * Usage:
 *   node scripts/migrate-llm-wiki.js              # Legacy migration in cwd
 *   node scripts/migrate-llm-wiki.js ~/my-wiki     # Legacy migration at path
 *   node scripts/migrate-llm-wiki.js --dry-run     # Preview without changes
 *   node scripts/migrate-llm-wiki.js --force       # Skip confirmation prompt
 *   node scripts/migrate-llm-wiki.js --fix-doubled # Flatten doubled .llm-wiki/.llm-wiki
 *   node scripts/migrate-llm-wiki.js --fix-doubled ~/  # …at a specific root
 */

const { createHash, randomUUID } = require("node:crypto");
const {
  closeSync,
  constants,
  existsSync,
  fstatSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  readlinkSync,
  renameSync,
  rmdirSync,
  statSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} = require("node:fs");
const { homedir } = require("node:os");
const { dirname, join, resolve } = require("node:path");

// ─── Helpers ────────────────────────────────────────────

const DRY_RUN = process.argv.includes("--dry-run");
const FORCE = process.argv.includes("--force");
const FIX_DOUBLED = process.argv.includes("--fix-doubled");

function resolvePositionalRoot(defaultRoot) {
  const positional = process.argv.slice(2).find((argument) => !argument.startsWith("--"));
  return positional ? resolve(process.cwd(), positional) : defaultRoot;
}

function log(action, ...args) {
  const prefix = DRY_RUN ? "[DRY-RUN]" : "[MIGRATE]";
  console.log(`${prefix} ${action}`, ...args);
}

const JOURNAL_NAME = "MIGRATION_TO_LLM_WIKI.json";
const JOURNAL_VERSION = 1;

function readRegularFile(path, encoding) {
  const flags =
    process.platform === "win32" ? "r" : constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0);
  const handle = openSync(path, flags);
  try {
    if (!fstatSync(handle).isFile()) throw new Error(`Expected regular file: ${path}`);
    return readFileSync(handle, encoding);
  } finally {
    closeSync(handle);
  }
}

function readEntry(path) {
  const flags =
    process.platform === "win32" ? "r" : constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0);
  let handle;
  try {
    handle = openSync(path, flags);
    const stat = fstatSync(handle);
    if (stat.isFile()) return { kind: "file", content: readFileSync(handle) };
    if (stat.isDirectory()) return { kind: "directory" };
    throw new Error(`Unsupported filesystem entry: ${path}`);
  } catch (error) {
    if ((error.code === "ELOOP" || error.code === "EPERM") && lstatSync(path).isSymbolicLink()) {
      return { kind: "symlink", target: readlinkSync(path) };
    }
    if (error.code === "EISDIR" && lstatSync(path).isDirectory()) return { kind: "directory" };
    throw error;
  } finally {
    if (handle !== undefined) closeSync(handle);
  }
}

function hashPath(path) {
  const hash = createHash("sha256");
  function part(value) {
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(value);
    const length = Buffer.allocUnsafe(8);
    length.writeBigUInt64BE(BigInt(bytes.length));
    hash.update(length);
    hash.update(bytes);
  }
  function visit(current, rel) {
    const entry = readEntry(current);
    if (entry.kind === "directory") {
      part("dir");
      part(rel);
      for (const child of readdirSync(current).sort())
        visit(join(current, child), join(rel, child));
    } else if (entry.kind === "file") {
      part("file");
      part(rel);
      part(entry.content);
    } else {
      part("symlink");
      part(rel);
      part(entry.target);
    }
  }
  visit(path, ".");
  return hash.digest("hex");
}

function assertNoSymlinkAncestors(path, boundary) {
  let current = resolve(path);
  const root = resolve(boundary);
  while (true) {
    const stat = lstatSync(current);
    if (stat.isSymbolicLink()) {
      throw new Error(`Symlinked migration path requires manual migration: ${current}`);
    }
    if (current === root) return;
    const parent = dirname(current);
    if (parent === current) throw new Error(`Migration path escapes root: ${path}`);
    current = parent;
  }
}

function assertSameDevice(path, device) {
  const stat = lstatSync(path);
  if (stat.dev !== device) throw new Error(`Cross-device migration entry is unsupported: ${path}`);
  if (stat.isSymbolicLink()) {
    throw new Error(`Symlinked migration entries require manual migration: ${path}`);
  }
  if (stat.isDirectory()) {
    for (const entry of readdirSync(path)) assertSameDevice(join(path, entry), device);
  }
}

function syncDirectory(path) {
  let handle;
  try {
    handle = openSync(path, "r");
    fsyncSync(handle);
  } catch (error) {
    if (process.platform !== "win32") throw error;
  } finally {
    if (handle !== undefined) closeSync(handle);
  }
}

function writeDurableFile(path, content, exclusive = false) {
  const handle = openSync(path, exclusive ? "wx" : "w");
  try {
    writeFileSync(handle, content, "utf8");
    fsyncSync(handle);
  } finally {
    closeSync(handle);
  }
  if (exclusive) syncDirectory(dirname(path));
}

function writeJournal(path, journal, exclusive = false) {
  const content = `${JSON.stringify(journal, null, 2)}\n`;
  const temporary = `${path}.${process.pid}-${randomUUID()}.tmp`;
  writeDurableFile(temporary, content, true);
  if (exclusive) {
    try {
      linkSync(temporary, path);
      syncDirectory(dirname(path));
    } finally {
      if (existsSync(temporary)) unlinkSync(temporary);
    }
    syncDirectory(dirname(path));
    return;
  }
  renameSync(temporary, path);
  syncDirectory(dirname(path));
}

function readJournal(path, root) {
  const journal = JSON.parse(readFileSync(path, "utf8"));
  if (
    journal.version !== JOURNAL_VERSION ||
    journal.root !== root ||
    !Array.isArray(journal.items) ||
    typeof journal.started !== "string" ||
    typeof journal.operation_id !== "string"
  ) {
    throw new Error(`Invalid migration journal: ${path}`);
  }
  return journal;
}

function moveNoClobber(item) {
  mkdirSync(dirname(item.dest), { recursive: true });
  if (item.type === "file") {
    // Hard-link creation is atomic and fails with EEXIST instead of replacing a raced file.
    linkSync(item.src, item.dest);
    syncDirectory(dirname(item.dest));
    unlinkSync(item.src);
    syncDirectory(dirname(item.src));
    return;
  }
  if (item.type === "symlink") {
    symlinkSync(readlinkSync(item.src), item.dest);
    syncDirectory(dirname(item.dest));
    unlinkSync(item.src);
    syncDirectory(dirname(item.src));
    return;
  }
  if (existsSync(item.dest)) throw new Error(`Destination appeared: ${item.dest}`);
  renameSync(item.src, item.dest);
  syncDirectory(dirname(item.dest));
  syncDirectory(dirname(item.src));
}

function moveTreeNoClobber(src, dest) {
  const stat = lstatSync(src);
  if (stat.isSymbolicLink()) {
    throw new Error(`Symlinked doubled-layout entries require manual migration: ${src}`);
  }
  if (stat.isFile()) {
    linkSync(src, dest);
    syncDirectory(dirname(dest));
    unlinkSync(src);
    syncDirectory(dirname(src));
    return;
  }
  if (!stat.isDirectory()) throw new Error(`Unsupported filesystem entry: ${src}`);

  mkdirSync(dest);
  syncDirectory(dirname(dest));
  const completed = [];
  try {
    for (const entry of readdirSync(src).sort()) {
      const child = { src: join(src, entry), dest: join(dest, entry) };
      moveTreeNoClobber(child.src, child.dest);
      completed.push(child);
    }
    rmdirSync(src);
    syncDirectory(dirname(src));
  } catch (error) {
    const rollbackErrors = [];
    for (const child of completed.reverse()) {
      try {
        moveTreeNoClobber(child.dest, child.src);
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError.message);
      }
    }
    try {
      rmdirSync(dest);
      syncDirectory(dirname(dest));
    } catch (rollbackError) {
      rollbackErrors.push(rollbackError.message);
    }
    const rollback = rollbackErrors.length ? ` Rollback errors: ${rollbackErrors.join("; ")}` : "";
    throw new Error(`${error.message}.${rollback}`);
  }
}

function restoreMove(item) {
  if (hashPath(item.dest) !== item.digest) {
    throw new Error(`Rollback destination changed: ${item.dest}`);
  }
  mkdirSync(dirname(item.src), { recursive: true });
  if (item.type === "file") {
    linkSync(item.dest, item.src);
    syncDirectory(dirname(item.src));
    unlinkSync(item.dest);
    syncDirectory(dirname(item.dest));
    return;
  }
  if (item.type === "symlink") {
    symlinkSync(readlinkSync(item.dest), item.src);
    syncDirectory(dirname(item.src));
    unlinkSync(item.dest);
    syncDirectory(dirname(item.dest));
    return;
  }
  if (existsSync(item.src)) throw new Error(`Rollback source exists: ${item.src}`);
  renameSync(item.dest, item.src);
  syncDirectory(dirname(item.src));
  syncDirectory(dirname(item.dest));
}

function completeOrResumeMove(item) {
  const sourceExists = existsSync(item.src);
  const destinationExists = existsSync(item.dest);

  if (sourceExists && destinationExists) {
    if (item.type === "file") {
      const source = statSync(item.src);
      const destination = statSync(item.dest);
      if (source.dev === destination.dev && source.ino === destination.ino) {
        if (hashPath(item.dest) !== item.digest) {
          throw new Error(`Moved destination changed: ${item.dest}`);
        }
        unlinkSync(item.src);
        syncDirectory(dirname(item.src));
        return;
      }
    }
    if (item.type === "symlink" && readlinkSync(item.src) === readlinkSync(item.dest)) {
      unlinkSync(item.src);
      syncDirectory(dirname(item.src));
      return;
    }
    throw new Error(`Both source and destination exist: ${item.src} → ${item.dest}`);
  }

  if (sourceExists) {
    if (hashPath(item.src) !== item.digest) throw new Error(`Source changed: ${item.src}`);
    moveNoClobber(item);
  } else if (!destinationExists) {
    throw new Error(`Both source and destination are missing: ${item.name}`);
  }

  if (hashPath(item.dest) !== item.digest)
    throw new Error(`Destination verification failed: ${item.dest}`);
}

function executeMovePlan(
  plan,
  newRoot,
  forwardingMarker,
  markerContent,
  journalPath,
  journal,
  ownerPath,
  resuming,
) {
  const moved = [];
  let ownsMarker = false;
  let ownsRoot = false;

  try {
    if (resuming) {
      try {
        mkdirSync(newRoot);
        ownsRoot = true;
        syncDirectory(dirname(newRoot));
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
      }
      let ownerContent;
      try {
        if (process.platform === "win32" && lstatSync(ownerPath).isSymbolicLink()) {
          throw new Error(`Migration owner marker is symlinked: ${ownerPath}`);
        }
        ownerContent = readRegularFile(ownerPath, "utf8");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      if (ownerContent !== undefined) {
        if (ownerContent !== `${journal.operation_id}\n`) {
          throw new Error(`Migration destination is owned by another operation: ${newRoot}`);
        }
        ownsRoot = true;
      } else if (!journal.finalizing) {
        if (readdirSync(newRoot).length > 0) {
          throw new Error(`Migration owner marker is missing: ${ownerPath}`);
        }
        writeDurableFile(ownerPath, `${journal.operation_id}\n`, true);
      }
    } else {
      // Reserve the whole destination namespace atomically after preflight.
      mkdirSync(newRoot);
      ownsRoot = true;
      syncDirectory(dirname(newRoot));
      writeDurableFile(ownerPath, `${journal.operation_id}\n`, true);
    }
    for (const item of plan) {
      log(`MOVE ${item.name}: ${item.src} → ${item.dest}`);
      const sourceExisted = existsSync(item.src);
      moved.push(item);
      completeOrResumeMove(item);
      if (!journal.completed.includes(item.name)) journal.completed.push(item.name);
      writeJournal(journalPath, journal);
      if (sourceExisted && process.env.LLM_WIKI_MIGRATION_PAUSE_AFTER === item.name) {
        process.kill(process.pid, "SIGSTOP");
      }
    }

    if (existsSync(forwardingMarker)) {
      if (readFileSync(forwardingMarker, "utf8") !== markerContent) {
        throw new Error(`Destination appeared: ${forwardingMarker}`);
      }
    } else {
      writeDurableFile(forwardingMarker, markerContent, true);
      ownsMarker = true;
    }
    log("CREATE forwarding marker: .wiki/MIGRATED_TO_LLM_WIKI.md");
    journal.finalizing = true;
    writeJournal(journalPath, journal);
    if (existsSync(ownerPath)) {
      unlinkSync(ownerPath);
      syncDirectory(dirname(ownerPath));
    }
    unlinkSync(journalPath);
    syncDirectory(dirname(journalPath));
  } catch (error) {
    const rollbackErrors = [];
    if (ownsMarker && existsSync(forwardingMarker)) {
      try {
        unlinkSync(forwardingMarker);
      } catch (unlinkError) {
        rollbackErrors.push(`marker cleanup: ${unlinkError.message}`);
      }
    }
    for (const item of moved.reverse()) {
      try {
        if (existsSync(item.dest) && !existsSync(item.src)) restoreMove(item);
      } catch (rollbackError) {
        rollbackErrors.push(`${item.name}: ${rollbackError.message}`);
      }
    }
    const rolledBack = plan.every((item) => existsSync(item.src) && !existsSync(item.dest));
    if (rollbackErrors.length === 0 && rolledBack) {
      try {
        if (existsSync(ownerPath)) {
          const owner = readFileSync(ownerPath, "utf8");
          if (owner === `${journal.operation_id}\n`) unlinkSync(ownerPath);
        }
        if (existsSync(journalPath)) unlinkSync(journalPath);
        syncDirectory(dirname(journalPath));
        if (ownsRoot) {
          try {
            rmdirSync(newRoot);
          } catch {
            // A non-empty directory is evidence preserved for manual recovery.
          }
        }
      } catch (journalError) {
        rollbackErrors.push(`journal cleanup: ${journalError.message}`);
      }
    } else if (!rolledBack) {
      rollbackErrors.push("rollback incomplete; migration journal retained");
    }
    const rollback = rollbackErrors.length
      ? ` Rollback errors: ${rollbackErrors.join("; ")}`
      : " All completed moves were rolled back.";
    throw new Error(`Migration move failed: ${error.message}.${rollback}`);
  }
}

// ─── Main ───────────────────────────────────────────────

async function fixDoubled() {
  // This is the directory that CONTAINS the outer .llm-wiki/.
  const parentRoot = resolvePositionalRoot(homedir());

  const outer = join(parentRoot, ".llm-wiki");
  const inner = join(outer, ".llm-wiki");
  const innerSentinel = join(inner, "config.json");

  if (existsSync(outer) && lstatSync(outer).isSymbolicLink()) {
    throw new Error(`Symlinked doubled-layout root requires manual migration: ${outer}`);
  }
  if (existsSync(inner) && lstatSync(inner).isSymbolicLink()) {
    throw new Error(`Symlinked doubled-layout root requires manual migration: ${inner}`);
  }

  console.log(`\n🔍 Scanning for doubled vault at: ${inner}\n`);

  if (!existsSync(innerSentinel)) {
    console.log("✅ No doubled vault detected (no inner .llm-wiki/config.json).");
    console.log(`   Outer: ${outer}`);
    console.log(`   Inner: ${inner}`);
    console.log("   Nothing to do.");
    process.exit(0);
  }

  const entries = readdirSync(inner);
  const plan = entries.map((entry) => {
    const src = join(inner, entry);
    const dest = join(outer, entry);
    return { src, dest, entry, collision: existsSync(dest) };
  });

  console.log("📋 Flatten plan:");
  console.log(`   ${inner}/  →  ${outer}/`);
  console.log("   ─────────────────────────────");
  for (const p of plan) {
    const status = p.collision ? "⚠️  COLLISION (skip)" : "✓ move";
    console.log(`   ${status}  ${p.entry}`);
  }

  if (plan.every((p) => p.collision)) {
    console.log("\n❌ Every inner entry collides with the outer vault. Resolve manually.");
    process.exit(1);
  }

  if (!FORCE && !DRY_RUN) {
    console.log("\n❓ Proceed with flatten? (y/N)");
    process.stdin.setRawMode?.(false);
    const answer = await new Promise((resolve) => {
      process.stdin.once("data", (data) => resolve(data.toString().trim().toLowerCase()));
    });
    if (answer !== "y" && answer !== "yes") {
      console.log("Migration cancelled.");
      process.exit(0);
    }
  }

  console.log("");
  let moved = 0;
  let skipped = 0;
  for (const p of plan) {
    if (p.collision || existsSync(p.dest)) {
      log(`SKIP ${p.entry} — destination already exists`);
      skipped++;
      continue;
    }
    log(`MOVE ${p.entry}: ${p.src} → ${p.dest}`);
    if (!DRY_RUN && process.env.LLM_WIKI_MIGRATION_PAUSE_BEFORE_DOUBLED === p.entry) {
      await new Promise((resolve) => process.stdin.once("data", resolve));
    }
    if (!DRY_RUN) moveTreeNoClobber(p.src, p.dest);
    moved++;
  }

  if (skipped === 0 && !DRY_RUN) {
    try {
      rmdirSync(inner);
      log(`RMDIR ${inner}`);
    } catch (err) {
      log(`RMDIR ${inner} failed: ${err.message} (left in place)`);
    }
  }

  console.log("");
  if (DRY_RUN) {
    console.log(`✅ Dry-run complete. Would move ${moved}, skip ${skipped}.`);
  } else {
    console.log(`✅ Flatten complete. Moved ${moved}, skipped ${skipped}.`);
    if (skipped > 0) {
      console.log(`   ${skipped} entry(ies) left in ${inner} due to collisions. Resolve manually.`);
    }
  }
}

async function main() {
  if (FIX_DOUBLED) {
    await fixDoubled();
    return;
  }

  // Determine root directory
  const root = resolvePositionalRoot(process.cwd());

  console.log(`\n🔍 Scanning for legacy wiki at: ${root}\n`);

  const oldSentinel = join(root, ".wiki", "config.json");
  const newRoot = join(root, ".llm-wiki");
  const newSentinel = join(newRoot, "config.json");
  const forwardingMarker = join(root, ".wiki", "MIGRATED_TO_LLM_WIKI.md");
  const journalPath = join(root, ".wiki", JOURNAL_NAME);
  const ownerPath = join(newRoot, ".migration-owner");
  const resuming = existsSync(journalPath);
  if (existsSync(join(root, ".wiki"))) {
    assertNoSymlinkAncestors(join(root, ".wiki"), root);
  }
  if (existsSync(newRoot)) {
    assertNoSymlinkAncestors(newRoot, root);
  }

  // Full plan is deterministic so a journal never controls arbitrary paths.
  const moves = [
    {
      src: join(root, ".wiki", "config.json"),
      dest: join(root, ".llm-wiki", "config.json"),
      type: "file",
      name: "config",
    },
    {
      src: join(root, ".wiki", "templates"),
      dest: join(root, ".llm-wiki", "templates"),
      type: "dir",
      name: "templates",
    },
    {
      src: join(root, "raw"),
      dest: join(root, ".llm-wiki", "raw"),
      type: "dir",
      name: "raw sources",
    },
    {
      src: join(root, "wiki"),
      dest: join(root, ".llm-wiki", "wiki"),
      type: "dir",
      name: "wiki pages",
    },
    {
      src: join(root, "meta"),
      dest: join(root, ".llm-wiki", "meta"),
      type: "dir",
      name: "metadata",
    },
    {
      src: join(root, "outputs"),
      dest: join(root, ".llm-wiki", "outputs"),
      type: "dir",
      name: "outputs",
    },
    {
      src: join(root, ".discoveries"),
      dest: join(root, ".llm-wiki", ".discoveries"),
      type: "dir",
      name: "discovery tracking",
    },
    {
      src: join(root, "WIKI_SCHEMA.md"),
      dest: join(newRoot, "WIKI_SCHEMA.md"),
      type: "file",
      name: "WIKI_SCHEMA",
    },
  ];

  let journal;
  let movePlan;
  if (resuming) {
    journal = readJournal(journalPath, root);
    const journalItems = new Map(
      journal.items.map((item) => {
        if (!item || typeof item.name !== "string" || typeof item.digest !== "string") {
          throw new Error(`Invalid migration journal item: ${journalPath}`);
        }
        return [item.name, item.digest];
      }),
    );
    movePlan = moves
      .filter((item) => journalItems.has(item.name))
      .map((item) => ({ ...item, digest: journalItems.get(item.name) }));
    if (movePlan.length !== journalItems.size || !Array.isArray(journal.completed)) {
      throw new Error(`Invalid migration journal plan: ${journalPath}`);
    }
    console.log(`↻ Resuming interrupted migration from ${journal.started}`);
  } else {
    if (!existsSync(oldSentinel)) {
      console.log("❌ No legacy wiki found (no .wiki/config.json). Nothing to migrate.");
      if (existsSync(newSentinel)) {
        console.log("   ✓ New-format wiki already exists at .llm-wiki/");
      } else {
        console.log("   No wiki vault found. Use wiki_bootstrap to create one.");
      }
      process.exit(0);
    }

    const rootDevice = statSync(root).dev;
    movePlan = moves
      .filter((item) => existsSync(item.src))
      .map((item) => {
        assertSameDevice(item.src, rootDevice);
        return { ...item, digest: hashPath(item.src) };
      });
    const conflicts = [
      ...movePlan.filter((item) => existsSync(item.dest)),
      ...(existsSync(newRoot) && !movePlan.some((item) => existsSync(item.dest))
        ? [{ dest: newRoot, name: "destination root" }]
        : []),
      ...(existsSync(forwardingMarker)
        ? [{ dest: forwardingMarker, name: "forwarding marker" }]
        : []),
    ];

    if (conflicts.length > 0) {
      console.log("\n❌ Migration blocked by destination conflicts:");
      for (const conflict of conflicts) console.log(`   ${conflict.dest}`);
      console.log("   No files were moved. Resolve these paths and rerun the migration.");
      process.exit(1);
    }

    const started = new Date().toISOString();
    journal = {
      version: JOURNAL_VERSION,
      operation_id: randomUUID(),
      root,
      started,
      items: movePlan.map(({ name, digest }) => ({ name, digest })),
      completed: [],
    };
  }

  // Print plan
  console.log("📋 Migration plan:");
  console.log("   Legacy format → New format");
  console.log("   ─────────────────────────────");
  for (const item of moves) {
    const included = movePlan.some((candidate) => candidate.name === item.name);
    console.log(`   ${included ? "✓" : "○"} ${item.name}: ${item.src} → ${item.dest}`);
  }

  // Remaining .wiki/ dir contents (after config + templates moved)
  const dotWikiContents = readdirSync(join(root, ".wiki")).filter(
    (entry) => entry !== "config.json" && entry !== "templates" && entry !== JOURNAL_NAME,
  );
  if (dotWikiContents.length > 0) {
    console.log(
      `\n   ⚠️ Additional .wiki/ contents (${dotWikiContents.length} items) will be left in place.`,
    );
    for (const c of dotWikiContents) {
      console.log(`      .wiki/${c}`);
    }
  }

  // Confirmation
  if (!FORCE && !DRY_RUN) {
    console.log("\n❓ Proceed with migration? (y/N)");
    // Read from stdin
    process.stdin.setRawMode?.(false);
    const answer = await new Promise((resolve) => {
      process.stdin.once("data", (data) => {
        resolve(data.toString().trim().toLowerCase());
      });
    });
    if (answer !== "y" && answer !== "yes") {
      console.log("Migration cancelled.");
      process.exit(0);
    }
  }

  const markerContent = [
    "# Migration Complete",
    "",
    `This vault was migrated to the new layout at \`.llm-wiki/\` on ${journal.started.split("T")[0]}.`,
    "",
    "The old `.wiki/` directory is kept as a forwarding marker.",
    "Remove it once you've verified everything works.",
    "",
    `New location: \`${newRoot}\``,
    "",
  ].join("\n");

  // Execute
  console.log("");
  if (!DRY_RUN) {
    if (!resuming) writeJournal(journalPath, journal, true);
    executeMovePlan(
      movePlan,
      newRoot,
      forwardingMarker,
      markerContent,
      journalPath,
      journal,
      ownerPath,
      resuming,
    );
  } else {
    for (const item of movePlan) log(`MOVE ${item.name}: ${item.src} → ${item.dest}`);
  }

  console.log("");
  if (DRY_RUN) {
    console.log("✅ Dry-run complete. No changes made.");
    console.log("   Run without --dry-run to perform the migration.");
  } else {
    console.log("✅ Migration complete!");
    console.log("");
    console.log("   What changed:");
    console.log("   • All wiki content moved under .llm-wiki/");
    console.log("   • Raw sources:       .llm-wiki/raw/");
    console.log("   • Wiki pages:        .llm-wiki/wiki/");
    console.log("   • Metadata:          .llm-wiki/meta/");
    console.log("   • Config/templates:  .llm-wiki/ (config.json, templates/)");
    console.log("   • Outputs:           .llm-wiki/outputs/");
    console.log("   • Forwarding marker: .wiki/MIGRATED_TO_LLM_WIKI.md");
    console.log("");
    console.log("   The old .wiki/ dir is kept as a marker. You can remove it once verified.");
    console.log("");
    console.log("   Update your gitignore:");
    console.log("     echo '.llm-wiki/' >> .gitignore");
    console.log("");
  }
}

main().catch((err) => {
  console.error("Migration error:", err);
  process.exit(1);
});
