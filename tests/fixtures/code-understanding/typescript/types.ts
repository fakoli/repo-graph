export type Value = number;
export function typed(value: Value): Value { return value; }
export function typedUse(value: Value): Value { return typed(value); }
