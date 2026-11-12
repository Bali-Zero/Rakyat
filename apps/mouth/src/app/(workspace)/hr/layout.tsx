"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Banknote,
  Gift,
  Calendar,
  Lock,
  Receipt,
  Settings,
  LayoutDashboard,
  Users,
} from "lucide-react";
import { api } from "@/lib/api";
import { useHrCapabilities } from "@/lib/hr/use-hr-capabilities";
import { isOwner } from "@/lib/auth/owner";
import { SubNav, type SubNavItem } from "@balizero/core";

type NavItem = SubNavItem & {
  adminOnly: boolean;
  /** Anyone with a People scope: full HR admins and the scoped read-only viewer. */
  peopleOnly?: boolean;
  accountingOnly?: boolean;
  ownerOnly?: boolean;
};

// "My HR" is the personal page for everyone. "People" is the HR console: everyone's Pay,
// Time and Me, for HR admins and for the scoped read-only viewer. The rest is an HR admin's
// tool: a member reaches the leave request form and a payslip from "My HR" (those routes
// stay open to them), not from the navigation. "Add employee" lives inside People.
const allNavItems: NavItem[] = [
  { href: "/hr/me", label: "My HR", icon: LayoutDashboard, adminOnly: false },
  {
    href: "/hr",
    label: "People",
    icon: Users,
    adminOnly: false,
    peopleOnly: true,
  },
  { href: "/hr/bonuses", label: "Bonuses", icon: Gift, adminOnly: true },
  { href: "/hr/payroll", label: "Payroll", icon: Banknote, adminOnly: true },
  { href: "/hr/leave", label: "Leave", icon: Calendar, adminOnly: true },
  { href: "/hr/settings", label: "Settings", icon: Settings, adminOnly: true },
  {
    href: "/accounting",
    label: "Accounting",
    icon: Receipt,
    adminOnly: false,
    accountingOnly: true,
  },
  {
    href: "/hr/owner-cashout",
    label: "Owner Cashout",
    icon: Lock,
    adminOnly: false,
    ownerOnly: true,
  },
];

export default function HRLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { capabilities } = useHrCapabilities();
  const [isOwnerUser, setIsOwnerUser] = useState(false);

  useEffect(() => {
    api
      .getProfile()
      .then((user) => setIsOwnerUser(isOwner(user?.email)))
      .catch(() => {});
  }, []);

  // Until the server answers, every member sees the member view and nothing more.
  const navItems = allNavItems.filter((item) => {
    if (item.adminOnly && !capabilities.hr_admin) return false;
    if (item.peopleOnly && !capabilities.people_scope) return false;
    if (item.accountingOnly && !capabilities.accounting_admin) return false;
    if (item.ownerOnly && !isOwnerUser) return false;
    return true;
  });

  const subNav = { items: navItems, pathname, rootHref: "/hr", linkAs: Link };

  return (
    <div className="flex flex-col md:flex-row h-full">
      {/* Desktop sidebar */}
      <nav className="w-56 border-r border-[var(--bz-border)] p-4 space-y-1 hidden md:block flex-shrink-0">
        <h2 className="text-sm font-semibold text-[var(--bz-text-2)] uppercase tracking-wider mb-4 px-3">
          HR / Payroll
        </h2>
        <SubNav {...subNav} variant="sidebar" />
      </nav>

      {/* Mobile navigation */}
      <nav className="md:hidden flex gap-1 overflow-x-auto px-4 py-2 border-b border-[var(--bz-border)] flex-shrink-0 [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]">
        <SubNav {...subNav} variant="chips" />
      </nav>

      {/* Main Content */}
      <div className="flex-1 overflow-auto p-6">{children}</div>
    </div>
  );
}
