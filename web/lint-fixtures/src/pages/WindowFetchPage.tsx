export async function WindowFetchPage() {
  const response = await window.fetch("/api/v1/papers");
  return response.ok;
}
