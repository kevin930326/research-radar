export async function getPapers() {
  const response = await fetch("/api/v1/papers");
  return response.json();
}
