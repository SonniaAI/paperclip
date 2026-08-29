#!/usr/bin/env node

import {
  applyCounterState,
  createEmptyCounterState,
  parseLedgerFile,
  readCounterState,
  type CounterState,
  writeCounterState,
} from "./index.js";

interface CliOptions {
  ledgerPath: string;
  statePath: string | null;
  write: boolean;
  printState: boolean;
}

function usage(): never {
  console.error(`Usage: dry-run.ts --ledger PATH [--state PATH] [--write] [--print-state]

Default mode is read-only. --write persists the resulting inspectable state to --state.
The ledger may be a JSON array/container or JSONL with an optional {"meta":{...}} row.`);
  process.exit(2);
}

function parseArgs(argv: string[]): CliOptions {
  let ledgerPath: string | null = null;
  let statePath: string | null = null;
  let write = false;
  let printState = false;

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--ledger") {
      ledgerPath = argv[++index] ?? null;
    } else if (arg === "--state") {
      statePath = argv[++index] ?? null;
    } else if (arg === "--write") {
      write = true;
    } else if (arg === "--print-state") {
      printState = true;
    } else if (arg === "--help" || arg === "-h") {
      usage();
    } else {
      console.error(`Unknown argument: ${arg}`);
      usage();
    }
  }

  if (!ledgerPath) {
    console.error("--ledger is required");
    usage();
  }
  if (write && !statePath) {
    console.error("--write requires --state PATH");
    usage();
  }
  return { ledgerPath, statePath, write, printState };
}

function stateSummary(state: CounterState) {
  return Object.fromEntries(
    Object.entries(state.lanes)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([lane, laneState]) => [lane, laneState]),
  );
}

const options = parseArgs(process.argv.slice(2));
const parsed = await parseLedgerFile(options.ledgerPath);
const initial = options.statePath
  ? await readCounterState(options.statePath)
  : createEmptyCounterState();
const result = applyCounterState(initial, parsed.records);

if (options.write) {
  await writeCounterState(options.statePath!, result.state);
}

const output: Record<string, unknown> = {
  schema: "son-1536.lane_failure_counter_dry_run.v1",
  mode: options.write ? "apply" : "dry-run",
  ledger_path: options.ledgerPath,
  state_path: options.statePath,
  input_records: parsed.records.length + parsed.invalid.length,
  normalized_records: parsed.records.length,
  invalid_records: parsed.invalid.length,
  applied_records: result.applied.length,
  duplicate_run_ids: result.duplicate_run_ids,
  already_processed_run_ids: result.already_processed_run_ids,
  conflicting_duplicate_run_ids: result.conflicting_duplicate_run_ids,
  skipped: result.skipped,
  lanes: stateSummary(result.state),
};
if (parsed.metadata) output.metadata = parsed.metadata;
if (options.printState) output.state = result.state;

console.log(JSON.stringify(output, null, 2));
