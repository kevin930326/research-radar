export async function loadPapers() {
  const response = await fetch("/api/v1/papers");
  return response.json();
}
