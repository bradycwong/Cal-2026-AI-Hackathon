// Shared Tailwind Play-CDN theme. Loaded once per page AFTER the cdn.tailwindcss.com
// script (which defines the `tailwind` global). Replaces the 90-line block that was
// previously copy-pasted inline into all 5 pages.
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      "colors": {
        "surface-bright": "#253050",
        "on-tertiary-fixed": "#1A0D00",
        "on-surface": "#E8EDF7",
        "on-secondary-fixed": "#0D1117",
        "error": "#FF6B6B",
        "tertiary-fixed": "#FFD580",
        "on-tertiary-container": "#FFD580",
        "tertiary-fixed-dim": "#F5A623",
        "surface": "#0D1117",
        "on-primary-fixed": "#051025",
        "on-tertiary": "#1A0D00",
        "on-tertiary-fixed-variant": "#7A4A00",
        "surface-container-high": "#1A2235",
        "primary": "#7BA7F5",
        "primary-container": "#2352C8",
        "on-error": "#1A0000",
        "inverse-surface": "#E8EDF7",
        "on-secondary": "#0D1117",
        "surface-tint": "#7BA7F5",
        "secondary-fixed": "#B8FF7C",
        "surface-container-lowest": "#080C14",
        "secondary": "#B8FF7C",
        "error-container": "#7A1515",
        "surface-container-low": "#111724",
        "on-surface-variant": "#8A96B0",
        "on-secondary-fixed-variant": "#3A7A1A",
        "tertiary": "#F5A623",
        "secondary-container": "#3A7A1A",
        "on-secondary-container": "#0D1117",
        "background": "#0D1117",
        "primary-fixed": "#C5D5F5",
        "on-primary-fixed-variant": "#1A3A7A",
        "surface-variant": "#1E2A3F",
        "inverse-on-surface": "#131929",
        "on-error-container": "#FFD5D5",
        "primary-fixed-dim": "#7BA7F5",
        "surface-container": "#131929",
        "inverse-primary": "#2352C8",
        "surface-container-highest": "#1E2A3F",
        "secondary-fixed-dim": "#8FD95A",
        "outline-variant": "#253050",
        "on-primary-container": "#E8EDF7",
        "tertiary-container": "#7A4A00",
        "outline": "#4A5570",
        "on-background": "#E8EDF7",
        "surface-dim": "#0D1117",
        "on-primary": "#051025"
      },
      "borderRadius": {
        "DEFAULT": "0.125rem",
        "lg": "0.25rem",
        "xl": "0.5rem",
        "full": "0.75rem"
      },
      "spacing": {
        "touch-target": "48px",
        "gutter": "24px",
        "unit": "8px",
        "stack-gap": "16px",
        "container-padding": "32px"
      },
      "fontFamily": {
        "display-timer": ["JetBrains Mono"],
        "headline-lg": ["DM Mono"],
        "headline-lg-mobile": ["DM Mono"],
        "headline-md": ["DM Mono"],
        "data-value": ["JetBrains Mono"],
        "body-md": ["DM Sans"],
        "data-label": ["JetBrains Mono"],
        "body-lg": ["DM Sans"]
      },
      "fontSize": {
        "headline-lg": ["32px", {"lineHeight": "40px", "fontWeight": "600"}],
        "headline-lg-mobile": ["24px", {"lineHeight": "32px", "fontWeight": "600"}],
        "headline-md": ["24px", {"lineHeight": "32px", "fontWeight": "500"}],
        "data-label": ["14px", {"lineHeight": "20px", "fontWeight": "500"}],
        "display-timer": ["72px", {"lineHeight": "80px", "letterSpacing": "-0.02em", "fontWeight": "700"}],
        "body-md": ["16px", {"lineHeight": "24px", "fontWeight": "400"}],
        "data-value": ["18px", {"lineHeight": "24px", "fontWeight": "600"}],
        "body-lg": ["20px", {"lineHeight": "30px", "fontWeight": "400"}]
      }
    },
  }
}
