function decodeLedger(entries) {
  // Decode ledger entries into their numeric amounts.
  function readAmount(entry) {
    return entry.amount;
  }
  return entries.map(readAmount);
}

function decodeLedgerGuide() {
  // Describe ledger fields without decoding entries.
  return "amount";
}

function invalidateTenantCache(cache, tenant) {
  // Invalidate cache entry for a tenant.
  delete cache[tenant];
}
