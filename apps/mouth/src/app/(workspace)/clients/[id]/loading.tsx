import { ClientDetailSkeleton } from "./components/ClientDetailSkeleton";

// Same skeleton the page shows while its profile loads, so a route
// transition and the data wait look like one continuous state.
export default function ClientDetailLoading() {
  return <ClientDetailSkeleton />;
}
