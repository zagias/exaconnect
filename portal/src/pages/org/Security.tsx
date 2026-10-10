import { lazy, Suspense } from "react";
import { PageHead } from "../../ui";

// The single sign-on screens live with Jibsy's code; they apply to the whole organisation.
const SignIn = lazy(() => import("../commai/settings/SignIn"));

/** Sign-in and security for the whole organisation (ADR 0043): single sign-on and the directory. */
export default function SecurityPage() {
  return (
    <>
      <PageHead title="Sign-in and security">
        How your people sign in to ExaCarib Connect and Jibsy: your own single sign-on, and keeping people in step
        with your directory. Each person&apos;s password and two-step sign-in are under Profile and sign-in.
      </PageHead>
      <Suspense fallback={<p className="muted">Loading…</p>}>
        <SignIn />
      </Suspense>
    </>
  );
}
