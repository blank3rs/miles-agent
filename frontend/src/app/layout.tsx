import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'Heso CEO',
  description: 'Autonomous CEO agent for Heso',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
