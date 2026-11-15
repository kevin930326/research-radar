export async function FetchPage() {
  const response = await fetch("/api/v1/papers");
  return response.ok;
}
