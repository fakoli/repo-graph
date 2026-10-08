// Synthetic source only: café, λ and 🌱 test UTF-8 offsets.
import { finish as importedFinish } from "./helpers";
import { added } from "./optional";

export function local(value: number): number { return value + 1; }
export function direct(): number { return local(1); }
export function imported(): number { return importedFinish(2); }
export function reference(): (value: number) => number { return local; }
export function valueAlias(): number {
  const nextStep = local;
  return nextStep(2);
}
export function shadow(): number {
  const local = (value: number): number => value + 100;
  return local(3);
}
export function callback(fn: (value: number) => number): number { return fn(4); }
export interface Worker { run(): number; }
export class First implements Worker { run(): number { return local(5); } }
export class Second implements Worker { run(): number { return importedFinish(6); } }
export function receiver(chooseFirst: boolean): number {
  const worker: Worker = chooseFirst ? new First() : new Second();
  return worker.run();
}
export function missing(): number { return added(7); }
export function dynamic(table: Record<string, (value: number) => number>, name: string): number { return table[name](8); }
export function café(value: number): number { return value - 1; }
export function unicodeCall(): number { return café(9); }
