const { fetch: send } = self;

export async function loadPapers() {
  const response = await send("/api/v1/papers");
  return response.json();
}
