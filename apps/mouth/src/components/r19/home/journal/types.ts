export type JournalDestinationStatus = "available" | "unavailable";

export interface JournalImage {
  readonly src: string;
  readonly alt: string;
  readonly sourceSrc?: string;
}

export interface JournalDate {
  readonly iso: string;
  readonly label: string;
}

export interface JournalArticle {
  readonly title: string;
  readonly slug: string;
  readonly image: JournalImage | null;
  readonly category: string | null;
  readonly date: JournalDate | null;
  readonly sourceUrl: string;
  readonly finalSourceUrl: string | null;
  readonly destinationStatus: JournalDestinationStatus;
  readonly localHref?: string;
  /**
   * The homepage-layout.json slot that placed this article on the home
   * (hero_main..hero_5, latest_1..latest_5, insight_1..insight_3). Rendered as
   * data-homepage-position on its links, which the News Room live verifier
   * reads; absent for an article that fills the Journal from the feed.
   */
  readonly homepagePosition?: string;
  readonly summary?: string;
  readonly featured?: boolean;
  readonly reading?: {
    readonly author?: { readonly name: string; readonly role?: string };
    readonly reviewedBy?: string;
    readonly updated?: JournalDate;
    readonly minutes?: number;
    readonly aiGenerated?: boolean;
    readonly disclosure?: string;
  };
  readonly editorial?: {
    readonly summary: string;
    readonly whyItMatters: string;
    readonly revision: number;
    readonly amended: boolean;
    readonly updatedAt: JournalDate;
    readonly evidence: readonly {
      publisher: string;
      citation: string | null;
      url: string | null;
    }[];
    readonly revisions: readonly {
      version: number;
      publishedAt: JournalDate;
    }[];
  };
}

export interface EditorialFeed {
  status:
    | "ready"
    | "empty"
    | "unavailable"
    | "withdrawn"
    | "unpublished"
    | "malformed";
}
