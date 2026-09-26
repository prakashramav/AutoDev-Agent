import SubmitForm from "@/components/SubmitForm";

export default function SubmitPage() {
  return (
    <div className="min-h-screen grid-bg px-8 py-8 flex items-start justify-center pt-20">
      <div className="w-full max-w-lg">
        <div className="text-center mb-8">
          <div className="w-12 h-12 mx-auto rounded-xl bg-gradient-to-br from-violet-600 to-blue-600 flex items-center justify-center mb-4 shadow-lg shadow-violet-900/40">
            <svg width="22" height="22" fill="none" stroke="white" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-white">New Agent Task</h1>
          <p className="text-sm text-slate-500 mt-2">
            The agent will clone the repo, understand the issue, write a fix, run
            tests, and open a PR — autonomously.
          </p>
        </div>
        <SubmitForm />
      </div>
    </div>
  );
}
