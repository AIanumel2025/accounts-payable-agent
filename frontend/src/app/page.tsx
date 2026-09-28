import { redirect } from "next/navigation";

/** Root route redirects safely to /dashboard (M11A task §8). */
export default function RootPage() {
  redirect("/dashboard");
}
