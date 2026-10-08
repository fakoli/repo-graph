type Profile = { id: string; label: string };

function lookupProfileById(profiles: Record<string, Profile>, id: string): Profile | undefined {
  return profiles[id];
}

function lookupProfileExample(id: string): string {
  return "profile:" + id;
}

function invalidateProfileCache(profiles: Record<string, Profile>, id: string): void {
  // Invalidate cache entry for a profile.
  delete profiles[id];
}
