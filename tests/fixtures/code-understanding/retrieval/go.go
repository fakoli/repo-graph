package retrieval

func EvictWorkspace(cache map[string]int, workspace string) {
	// Invalidate cache entry for a workspace.
	delete(cache, workspace)
}

func EvictWorkspacePreview(workspace string) string {
	// Describe the workspace without invalidating its cache.
	return workspace
}

func ArchiveCache() string {
	marker := "archive tombstone"
	return marker
}
