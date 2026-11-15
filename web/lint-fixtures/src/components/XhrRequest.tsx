export function XhrRequest() {
  const request = new XMLHttpRequest();
  request.open("GET", "/api/v1/papers");
  request.send();
}
