// 照規則寫：頁面只 import 產生的型別，不能被誤擋。
import type { MemberResponse } from "../generated/model";

export function ImportGeneratedTypePage({ member }: { member: MemberResponse }) {
  return <p lang="en">{member.member_id}</p>;
}
