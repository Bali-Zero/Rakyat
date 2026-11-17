import { Skeleton } from "@/components/ui/skeleton";

export default function RelatedSubjectsLoading() {
  return (
    <div className="mx-auto max-w-6xl space-y-4 px-4 py-8">
      <Skeleton variant="text" width={260} height={28} />
      <div className="space-y-3 border-t p-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} variant="text" width="100%" height={20} />
        ))}
      </div>
    </div>
  );
}
