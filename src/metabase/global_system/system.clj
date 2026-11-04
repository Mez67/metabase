(ns metabase.global-system.system
  "Storage for the global system, with one value per component, addressed by a namespace-qualified keyword.

   A dynamic var has two slots: a root shared by every thread, and a per-thread binding that shadows it.
   This namespace keeps the same two slots for every component.
   All roots live in one atom, so [[snapshot]] reads every root at a single instant.
   Each thread's bindings are an overlay holding only the components bound on that thread, never a copy of the system.
   An unbound component reads straight through to its root, so binding or changing one component never affects
   another.

   A derived component, made with [[derived-component-handle]], is read-only: its value is made from other components'
   current values, so it follows their bindings without one of its own."
  (:require
   [clojure.pprint :as pprint]
   [metabase.global-system.mutable-component :as mc])
  (:import
   (clojure.lang IDeref)
   (java.io Writer)
   (java.util.concurrent ConcurrentHashMap)
   (java.util.function Function)))

(set! *warn-on-reflection* true)

(defonce ^{:doc "Component key -> root value, shared by every thread."
           :private true}
  roots
  (atom {}))

;; The dynamic var is the one place thread-local component values live, which is what lets a binding of one component
;; leave every other component reading its root.
#_{:clj-kondo/ignore [:metabase/discourage-dynamic-vars]}
(def ^{:doc "Component key -> atom holding that component's value on this thread and on threads that inherit its
             bindings.
             Holds only the components bound here; everything else resolves against [[roots]]."
       :dynamic true
       :private true}
  *bound-boxes*
  {})

(defn snapshot
  "Every component's value as this thread sees it, so `(get (snapshot) k)` agrees with dereferencing the handle for `k`.
   Roots are read in one step, so components nobody has bound here are consistent with each other.
   Bound components are read one at a time, and a thread that inherited a binding can change one mid-read."
  []
  (merge @roots (update-vals *bound-boxes* deref)))

(defn- root-value
  "The root value of `k` in the roots map `rs`.
   Throws if `k` was never installed with [[mc/alter-root]]."
  [rs k]
  (if-let [entry (find rs k)]
    (val entry)
    (throw (ex-info (str "Component has no value: " k)
                    {:component  k
                     :components (sort (keys rs))}))))

(defonce ^{:doc "Component key -> the lock for that component's root slot."
           :private true}
  root-locks
  (ConcurrentHashMap.))

(defn- root-lock
  "The lock for the root slot of `k`, the same object every time for a given `k`."
  [k]
  (.computeIfAbsent ^ConcurrentHashMap root-locks k (reify Function (apply [_ _] (Object.)))))

(defn- current-value
  "The value of `k` visible to this thread: the bound one if `k` is bound here, otherwise the root."
  [k]
  (if-let [box (get *bound-boxes* k)]
    @box
    (root-value @roots k)))

;; A deftype, not a record: a record would be a map as well as an IDeref, and a handle is neither a value nor a
;; lookup.
(deftype ComponentHandle [k]
  IDeref
  (deref [_] (current-value k))

  mc/MutableComponentHandle
  (binding [_ new-value thunk]
    (clojure.core/binding [*bound-boxes* (assoc *bound-boxes* k (atom new-value))]
      (thunk)))
  (swap-vals!* [_ f args]
    (if-let [box (get *bound-boxes* k)]
      (apply swap-vals! box f args)
      (let [[before after] (swap-vals! roots (fn [rs] (assoc rs k (apply f (root-value rs k) args))))]
        [(get before k) (get after k)])))
  (alter-root [_ new-value]
    (let [[before _] (swap-vals! roots assoc k new-value)]
      (get before k)))
  (root-entry [_]
    (find @roots k))
  (remove-root [_]
    (swap! roots dissoc k)
    nil)
  (without-binding [_ thunk]
    (clojure.core/binding [*bound-boxes* (dissoc *bound-boxes* k)]
      (thunk)))
  (slot-lock [_]
    (or (get *bound-boxes* k)
        (root-lock k))))

;; Prints the key alone. The IDeref defaults would deref the handle, which throws when the component has no value.
(defmethod print-method ComponentHandle
  [^ComponentHandle handle ^Writer w]
  (.write w (str "#<ComponentHandle " (pr-str (.-k handle)) ">")))

(defmethod pprint/simple-dispatch ComponentHandle
  [handle]
  (print-method handle *out*))

(defn mutable-component-handle
  "Make a handle for the component stored under `k` in the global system.
   With `init-root`, a function of no arguments, also install its result as the root if the component has none, so
   reloading the namespace that makes the handle leaves an installed root alone."
  ([k]
   (ComponentHandle. k))
  ([k init-root]
   (let [handle (ComponentHandle. k)
         lock   (root-lock k)]
     (locking lock
       (when-not (mc/root-entry handle)
         (mc/alter-root handle (init-root))))
     handle)))

;;; -------------------------------------------- Derived components --------------------------------------------

(def ^:private max-derived-values
  "How many dependency values a derived component remembers a value for. The most recently made value is kept first,
  and the oldest goes once there are more."
  8)

(defn- derived-entry
  "The `[deps value]` entry in `entries` made for dependency values identical, one by one, to `deps`, or nil.
  Returns the entry rather than the value, so a value of nil or false is still found."
  [entries deps]
  (some (fn [[entry-deps :as entry]]
          (when (every? true? (map identical? entry-deps deps))
            entry))
        entries))

(deftype DerivedHandle [k dep-handles make entries]
  IDeref
  (deref [_]
    (let [deps  (mapv deref dep-handles)
          found (derived-entry @entries deps)]
      (second
       (or found
           (derived-entry (swap! entries (fn [es]
                                           (if (derived-entry es deps)
                                             es
                                             (into [[deps (apply make deps)]] (take (dec max-derived-values)) es))))
                          deps))))))

(defmethod print-method DerivedHandle
  [^DerivedHandle handle ^Writer w]
  (.write w (str "#<DerivedHandle " (pr-str (.-k handle)) ">")))

(defmethod pprint/simple-dispatch DerivedHandle
  [handle]
  (print-method handle *out*))

(defn derived-component-handle
  "Make a read-only handle for a component made from the values of other components.
   Dereferencing it reads each of `dep-handles` as this thread sees them, and returns the value `make` returned for
   those values, calling `(make dep-value ...)` the first time this combination is seen.
   Values are told apart by identity, so a binding of a dependency, or a write that replaces its value, gets a
   fresh derived value, and the derived value follows bindings of its dependencies with no binding of its own.
   Only the last few combinations are remembered, so `make` must build something that is safe to rebuild, such as a
   cache, and it can run more than once for the same values when threads race, keeping one result."
  [k dep-handles make]
  (DerivedHandle. k (vec dep-handles) make (atom [])))
