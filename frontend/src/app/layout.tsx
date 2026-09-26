import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Sidebar from "@/components/Sidebar";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });

export const metadata: Metadata = {
  title: "AutoDev Agent — AI Software Engineer",
  description:
    "Autonomous AI agent that takes a GitHub issue and opens a Pull Request — no human in the loop.",
  keywords: ["AI", "coding agent", "GitHub", "pull request", "automation"],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="bg-[#07070f] text-slate-100 antialiased min-h-screen flex">
        <Sidebar />
        <main className="flex-1 ml-64 min-h-screen">{children}</main>
      </body>
    </html>
  );
}
