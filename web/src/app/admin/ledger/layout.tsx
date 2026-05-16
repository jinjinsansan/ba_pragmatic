import { redirect } from 'next/navigation'

export default function AdminLedgerLayout({ children }: { children: React.ReactNode }) {
  redirect('/admin')
  return children
}
