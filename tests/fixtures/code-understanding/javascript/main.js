// Synthetic source only: café, λ and 🌱 test UTF-8 offsets.
import { finish as importedFinish } from "./helpers.js";
import { added } from "./optional.js";

export function local(value) { return value + 1; }
export function direct() { return local(1); }
export function imported() { return importedFinish(2); }
export function reference() { return local; }
export function valueAlias() {
  const nextStep = local;
  return nextStep(2);
}
export function shadow() {
  const local = (value) => value + 100;
  return local(3);
}
export function callback(fn) { return fn(4); }
export class First { run() { return local(5); } }
export class Second { run() { return importedFinish(6); } }
export function receiver(chooseFirst) {
  const worker = chooseFirst ? new First() : new Second();
  return worker.run();
}
export function missing() { return added(7); }
export function dynamic(table, name) { return table[name](8); }
export function café(value) { return value - 1; }
export function unicodeCall() { return café(9); }
