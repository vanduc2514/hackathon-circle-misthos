// MiroFish Autonomous SWE Patch for #182: Overview becomes signed-in user's dashboard
// Fixes https://github.com/vanduc2514/hackathon-circle-misthos/issues/182

export interface UserDashboardRoleProps {
  role: 'publisher' | 'contributor';
  partyAddress: string;
}

export function resolveUserDashboardView(userRole: string, activeClaims: any[], fundedIssues: any[]) {
  if (userRole === 'publisher') {
    return {
      title: "Publisher Control Center",
      primaryMetric: fundedIssues.length,
      metricLabel: "Active Funded Issues Awaiting Review",
      items: fundedIssues
    };
  }
  return {
    title: "Contributor Earnings & Claim Radar",
    primaryMetric: activeClaims.length,
    metricLabel: "Claimed Issues & Payouts Due",
    items: activeClaims
  };
}
