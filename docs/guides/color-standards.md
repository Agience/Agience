# Color Standardization Guide

Status: **Reference**
Date: 2026-04-01

## Overview
All colors in the application should use standard CSS color names (purple, blue, green, amber, red, gray) to ensure consistency across the UI.

## Color palette

### Primary Actions - Purple
- **Use for**: Main action buttons, active states, primary focus
- **Shades**: `purple-600` (default), `purple-700` (hover)
- **Examples**: New button, active view toggle, primary links

### Secondary Actions - Blue  
- **Use for**: Secondary action buttons, informational elements
- **Shades**: `blue-600` (default), `blue-700` (hover)
- **Examples**: Commit button, save actions, secondary CTAs

### Success/Draft State - Green
- **Use for**: Success messages, draft artifact states, positive indicators
- **Shades**: `green-600` (default), `green-50` (backgrounds)
- **Examples**: "Draft" artifact badges, success toasts

### Warning State - Amber
- **Use for**: Warning messages, caution indicators
- **Shades**: `amber-600` (default), `amber-50` (backgrounds)
- **Examples**: Warning toasts

### Danger/Archived State - Red
- **Use for**: Error messages, delete actions, archived artifact states
- **Shades**: `red-600` (default), `red-50` (backgrounds)
- **Examples**: "Archived" artifact badges, delete buttons, error toasts

### Neutral - Gray
- **Use for**: Borders, backgrounds, disabled states, secondary text
- **Shades**: `gray-200` (borders), `gray-600` (text), `gray-100` (hover)
- **Examples**: Artifact borders, disabled buttons, secondary text

## Usage examples

### Buttons
```tsx
// Primary action
className="bg-purple-600 hover:bg-purple-700 text-white"

// Secondary action  
className="bg-blue-600 hover:bg-blue-700 text-white"

// Ghost/Neutral
className="border border-gray-300 hover:bg-gray-100"

// Icon button
className="p-2 text-gray-600 hover:bg-gray-100 rounded"
```

### Artifact States
```tsx
// Draft card
className="border-2 border-green-300 bg-green-50"

// Committed card
className="border border-gray-300"

// Archived card  
className="border-2 border-red-300 bg-red-50 opacity-60"
```

### Active States
```tsx
// Active view toggle (purple)
className={viewMode === 'grid' ? 'bg-purple-600 text-white' : 'text-gray-600'}

// Active filter (purple)
className={isActive ? 'text-purple-600' : 'text-gray-600'}
```

## Component-specific colors

### BrowserHeader
- View Toggle Active: `purple-600`
- New Button: `purple-600`
- Commit Button: `blue-600`
- Icon Buttons: `gray-600` with `gray-100` hover
- Dropdowns: `gray-300` borders

### Sidebar
- Active Workspace: `purple-600` background
- Hover States: `gray-100`
- Icons: `gray-600`

### Artifacts
- Draft: `green-50` bg, `green-300` border
- Committed: default border (`gray-300`)
- Archived: `red-50` bg, `red-300` border
- Selected: `purple-100` bg, `purple-300` border

### Modals/Dialogs
- Primary Action: `purple-600`
- Cancel/Close: `gray-300` border
- Overlay: `gray-900` at 50% opacity

## Import constants

For consistent colors, import from `src/constants/colors.ts`:

```tsx
import { ButtonStyles, CardStateColors, TextColors } from '@/constants/colors';

// Use predefined button styles
<button className={ButtonStyles.primary}>New</button>
<button className={ButtonStyles.secondary}>Commit</button>

// Use card state colors
<div className={CardStateColors.draft.bg}>...</div>
```

## Accessibility

All color combinations meet WCAG 2.1 AA standards for contrast:
- Purple-600 on white: 4.6:1 ✓
- Blue-600 on white: 4.5:1 ✓
- Text colors use 600+ shades for sufficient contrast
- Disabled states use reduced opacity, not just color

## Notes

- Always use Tailwind's numbered shades (50, 100, 200, etc.)
- Avoid arbitrary colors like `#8B5CF6` - use Tailwind classes
- For hover states, use the next darker shade (600 → 700)
- For disabled states, use lighter shades (600 → 400)